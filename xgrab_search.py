"""XVidGrab Desktop - search-page collector (Edge automation).

Paste an X (Twitter) search link (x.com/search?q=...) and the app opens the
page in a real Edge window, auto-scrolls it to the very end collecting every
video post along the way, then resolves each collected post through the
public syndication engine so it lands in the normal download list.

Why a browser: X requires a logged-in session for search results and has no
public search API, so the page itself is the only reliable data source. The
collector runs in its own persistent Edge profile (~/XVidGrab-EdgeProfile)
- log in once when asked and every later run reuses that session.

The DOM harvesting is the desktop port of the XVidGrab browser-extension
v1.2.0 search fix: search-page videos do NOT autoplay (the videoPlayer
container never mounts on unplayed cards), so cards are matched on the
videoComponent wrapper / playButton glyph / <video> hints and tweet ids are
recovered from /status/<id>/video/ links when that is the only shape an
unplayed tile exposes.

Developed by www.3SVerse.com - part of the 3SVerse suite.
MIT licensed - see LICENSE.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time

import xgrab_core as core

__all__ = [
    "is_search_url",
    "SearchCollector",
    "resolve_ids",
    "COLLECT_JS",
    "STATE_JS",
]

# x.com/search?q=...  |  x.com/i/search/...  |  x.com/hashtag/...
# |  x.com/explore  (accepts twitter.com, www., mobile., no scheme)
_SEARCH_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.)?(?:twitter|x)\.com"
    r"(/(?:i/)?(?:search|hashtag|explore)[^\s\"'<>]*)",
    re.IGNORECASE,
)

# Scroll loop tuning (same shape as the extension's Fetch All)
SCROLL_STEP_SLEEP = 0.65        # seconds between scroll steps
STABLE_ROUNDS = 10              # ~6.5s with no growth = feed end
HARD_CAP_SECONDS = 12 * 60      # safety cap: 12 minutes
LOGIN_WAIT_SECONDS = 10 * 60    # wait up to 10 min for the user to log in
RESOLVE_DELAY = 0.2             # pause between syndication lookups
RESOLVE_BACKOFF = 5.0           # extra pause when X starts rate-limiting
RESOLVE_CHUNK = 10              # videos per on_items callback

# Persistent automation profile - log in once, reused by every later run.
PROFILE_DIR = os.path.join(os.path.expanduser("~"), "XVidGrab-EdgeProfile")


def is_search_url(text: str) -> str | None:
    """Return the canonical search URL if *text* contains an X search link.

    ``https://x.com/search?q=backshots&src=recent_search_click`` and friends
    (/i/search, /hashtag/x, /explore, twitter.com variants, missing scheme)
    all normalise to an https://x.com/... URL; anything else returns None.
    """
    if not text:
        return None
    m = _SEARCH_URL_RE.search(text)
    if not m:
        return None
    path = m.group(1)
    if len(path) > 3 and path[:3].lower() == "/i/":
        path = path[2:]
    return f"https://x.com{path}"


# --------------------------------------------------------------------------
# JavaScript injected into the page (kept as constants so tests can lint them)
# --------------------------------------------------------------------------

# Harvest every video card currently mounted in the timeline.  X virtualises
# its feed - articles scrolled far past get unmounted - so this runs after
# EVERY scroll step and the Python side accumulates + de-duplicates ids.
COLLECT_JS = r"""
var out = [];
var hints = "[data-testid='videoPlayer'],[data-testid='videoComponent'],"
          + "[data-testid='playButton'],video";
var arts = document.querySelectorAll("article[data-testid='tweet']");
for (var i = 0; i < arts.length; i++) {
  var art = arts[i];
  try {
    if (!art.querySelector(hints)) continue;
    var id = null, user = "";
    var timeEl = art.querySelector("time");
    var tLink = timeEl ? timeEl.closest("a[href*='/status/']") : null;
    if (tLink) {
      var m0 = /\/([A-Za-z0-9_]{1,20})\/status\/(\d+)/.exec(
        tLink.getAttribute("href") || "");
      if (m0) { id = m0[2]; user = m0[1]; }
    }
    if (!id) {
      var links = art.querySelectorAll("a[href*='/status/']");
      for (var j = 0; j < links.length; j++) {
        var href = links[j].getAttribute("href") || "";
        if (/\/(analytics|photo)\//.test(href)) continue;
        var m1 = /\/([A-Za-z0-9_]{1,20})\/status\/(\d+)/.exec(href);
        if (m1) { id = m1[2]; user = m1[1]; break; }
      }
    }
    /* unplayed search tiles often expose ONLY /status/<id>/video/1 */
    if (!id) {
      var vlinks = art.querySelectorAll("a[href*='/video/']");
      for (var k = 0; k < vlinks.length; k++) {
        var m2 = /\/([A-Za-z0-9_]{1,20})\/status\/(\d+)\/(?:video|photo)\//.exec(
          vlinks[k].getAttribute("href") || "");
        if (m2) { id = m2[2]; user = m2[1]; break; }
      }
    }
    if (!id) continue;
    var txt = (art.innerText || "").replace(/\s+/g, " ").slice(0, 140);
    out.push({ id: id, user: user, text: txt });
  } catch (e) { /* DOM surprises - never break the page */ }
}
var h = document.scrollingElement
  ? document.scrollingElement.scrollHeight
  : document.documentElement.scrollHeight;
var login = !!document.querySelector(
  "a[data-testid='loginButton'], input[autocomplete='username']");
return JSON.stringify({ n: out.length, h: h, login: login, items: out });
"""

# Quick page-state probe used while waiting for login / first results.
STATE_JS = r"""
var arts = document.querySelectorAll("article[data-testid='tweet']").length;
var login = !!document.querySelector(
  "a[data-testid='loginButton'], input[autocomplete='username']");
var h = document.scrollingElement
  ? document.scrollingElement.scrollHeight
  : document.documentElement.scrollHeight;
return JSON.stringify({ articles: arts, login: login, h: h });
"""


def _resolve_one(tid: str) -> list[core.VideoItem]:
    """Syndication lookup for a single tweet id ([] on soft failures)."""
    try:
        data = core.fetch_tweet(tid)
    except core.XGrabError as exc:
        msg = str(exc)
        if "limiting" in msg:  # HTTP 429 - back off and retry once
            time.sleep(RESOLVE_BACKOFF)
            try:
                data = core.fetch_tweet(tid)
            except core.XGrabError:
                return []
        else:
            return []  # deleted / private / photo-only post - skip
    try:
        return core.parse_videos(data, tid)
    except Exception:
        return []


def resolve_ids(
    ids: list[str],
    progress=None,
    on_items=None,
    stop_event: threading.Event | None = None,
) -> list[core.VideoItem]:
    """Turn collected tweet ids into VideoItems via the syndication engine.

    Shared by both collectors: the in-app webview (Qt build) and the Edge
    fallback.  Emits *on_items* in chunks of :data:`RESOLVE_CHUNK` videos so
    the GUI can render results while the rest resolve.
    """
    progress = progress or (lambda msg: None)
    on_items = on_items or (lambda chunk: None)
    stop_event = stop_event or threading.Event()
    total = len(ids)
    items: list[core.VideoItem] = []
    chunk: list[core.VideoItem] = []
    for i, tid in enumerate(ids, start=1):
        if stop_event.is_set():
            break
        progress(f"Resolving videos {i}/{total}...")
        got = _resolve_one(tid)
        if got:
            items.extend(got)
            chunk.extend(got)
            if len(chunk) >= RESOLVE_CHUNK:
                on_items(list(chunk))
                chunk = []
        time.sleep(RESOLVE_DELAY)
    if chunk:
        on_items(list(chunk))
    progress(f"Done - {len(items)} videos ready for download")
    return items


class SearchCollector:
    """Open an X search page in Edge, scroll it to the end, resolve videos.

    Parameters
    ----------
    url:
        Canonical search URL (see :func:`is_search_url`).
    progress:
        ``callable(str)`` - live status lines ("Scrolling... 42 videos").
    on_items:
        ``callable(list[core.VideoItem])`` - invoked with small chunks of
        resolved videos so the GUI can accumulate them as we go.
    stop_event:
        ``threading.Event`` - set it to stop scrolling/resolving early and
        still get everything collected so far.
    """

    def __init__(
        self,
        url: str,
        progress=None,
        on_items=None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.url = url
        self.progress = progress or (lambda msg: None)
        self.on_items = on_items or (lambda items: None)
        self.stop_event = stop_event or threading.Event()
        # id -> {"user": str, "text": str} ; insertion order preserved
        self.found: dict[str, dict] = {}

    # ------------------------------------------------------------ driver
    def _edge_paths(self) -> list[str]:
        return [
            shutil.which("msedge") or "",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            shutil.which("microsoft-edge") or "",
            "/usr/bin/microsoft-edge",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]

    def _check_edge(self) -> None:
        if any(p and os.path.isfile(p) for p in self._edge_paths()):
            return
        raise core.XGrabError(
            "Microsoft Edge was not found.\n"
            "Search-page fetching opens Edge to load the results - install "
            "Edge (microsoft.com/edge) and try again."
        )

    def _create_driver(self):
        """Build the Edge driver (selenium imported lazily)."""
        try:
            from selenium import webdriver
            from selenium.webdriver.edge.options import Options as EdgeOptions
        except ImportError as exc:  # pragma: no cover - packaging regression
            raise core.XGrabError(
                "The search collector needs the 'selenium' package.\n"
                "Reinstall XVidGrab Desktop or run: pip install selenium"
            ) from exc

        self._check_edge()

        options = EdgeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")
        options.add_argument("--profile-directory=Default")
        # hide the automation fingerprint (same recipe as the VidaPay tools)
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--disable-blink-features=AutomationControlled")

        try:
            driver = webdriver.Edge(options=options)
        except Exception as exc:
            raise core.XGrabError(
                "Could not start Microsoft Edge for the search page.\n"
                f"Details: {exc}"
            ) from exc
        driver.set_page_load_timeout(60)
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": (
                        "Object.defineProperty(navigator,'webdriver',"
                        "{get:()=>undefined,configurable:true});"
                    )
                },
            )
        except Exception:
            pass  # cosmetic hardening only
        return driver

    # ------------------------------------------------------------ helpers
    def _run_js(self, driver, script: str):
        raw = driver.execute_script("return " + script.strip())
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return {}

    def _harvest(self, driver) -> tuple[int, int]:
        """One COLLECT_JS pass; merges results into self.found."""
        data = self._run_js(driver, COLLECT_JS) or {}
        for rec in data.get("items") or []:
            tid = str(rec.get("id") or "")
            if not tid.isdigit():
                continue
            if tid not in self.found:
                self.found[tid] = {
                    "user": rec.get("user") or "",
                    "text": rec.get("text") or "",
                }
        return int(data.get("n") or 0), int(data.get("h") or 0)

    def _wait_for_results(self, driver) -> bool:
        """Wait until the timeline renders (logging in if the wall shows)."""
        deadline = time.monotonic() + LOGIN_WAIT_SECONDS
        told_login = False
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                return False
            state = self._run_js(driver, STATE_JS) or {}
            if int(state.get("articles") or 0) > 0:
                return True
            if state.get("login") and not told_login:
                told_login = True
                self.progress(
                    "X login required - log in inside the Edge window "
                    "(one time only), the collector keeps watching..."
                )
            time.sleep(2.0)
        return False

    # ------------------------------------------------------------ main
    def collect(self) -> list[core.VideoItem]:
        """Run the full collection.  Raises XGrabError on hard failures."""
        driver = None
        try:
            self.progress("Opening the search page in Edge...")
            driver = self._create_driver()
            try:
                driver.get(self.url)
            except Exception as exc:
                raise core.XGrabError(
                    f"The search page did not load: {exc}"
                ) from exc

            if not self._wait_for_results(driver):
                if self.stop_event.is_set():
                    return self._resolve_all()
                raise core.XGrabError(
                    "No search results appeared in Edge.\n"
                    "Log in to X in the Edge window when it asks, then "
                    "press Fetch videos again."
                )

            # -- auto-scroll to the very end of the feed ------------------
            self.progress("Scrolling the page - collecting videos...")
            started = time.monotonic()
            last_h = -1
            last_count = -1
            stable = 0
            while not self.stop_event.is_set():
                if time.monotonic() - started > HARD_CAP_SECONDS:
                    self.progress("Safety cap reached - using what we have.")
                    break
                driver.execute_script(
                    "window.scrollBy(0, Math.max(400, "
                    "Math.round(window.innerHeight * 0.9)));"
                )
                time.sleep(SCROLL_STEP_SLEEP)
                self._harvest(driver)
                h = self._page_height(driver)
                if h == last_h and len(self.found) == last_count:
                    stable += 1
                else:
                    stable = 0
                last_h = h
                last_count = len(self.found)
                if stable >= STABLE_ROUNDS:
                    break
                if last_count and last_count % 5 == 0:
                    self.progress(
                        f"Scrolling... {len(self.found)} video post"
                        f"{'s' if len(self.found) != 1 else ''} found"
                    )
            try:
                driver.execute_script("window.scrollTo(0, 0);")
            except Exception:
                pass

            if not self.found:
                if self.stop_event.is_set():
                    return []
                raise core.XGrabError(
                    "Scrolled the whole page but found no video posts.\n"
                    "The search results may contain no videos."
                )
            return self._resolve_all()
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    def _page_height(self, driver) -> int:
        try:
            return int(
                driver.execute_script(
                    "return document.scrollingElement"
                    " ? document.scrollingElement.scrollHeight : 0;"
                )
                or 0
            )
        except Exception:
            return 0

    def _resolve_all(self) -> list[core.VideoItem]:
        """Turn every collected tweet id into VideoItems via syndication."""
        return resolve_ids(
            list(self.found.keys()),
            progress=self.progress,
            on_items=self.on_items,
            stop_event=self.stop_event,
        )
