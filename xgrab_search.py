"""XVidGrab Desktop - page collector (Edge automation fallback).

Paste ANY x.com link and the app opens the page in a real Edge window,
auto-scrolls it to the very end collecting every video post along the way,
then resolves each collected post through the public syndication engine so
it lands in the normal download list. Profile timelines, /media tabs,
search results, hashtag and explore pages all work; the primary (Qt) build
collects inside the app's own browser window instead and additionally
supports DM chat links, which need the authenticated in-app session.

Why a browser: X requires a logged-in session for timelines and search and
has no public timeline API, so the page itself is the only reliable data
source. The collector runs in its own persistent Edge profile
(~/XVidGrab-EdgeProfile) - log in once when asked and every later run
reuses that session.

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
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import xgrab_core as core

__all__ = [
    "is_search_url",
    "is_chat_url",
    "page_kind",
    "SearchCollector",
    "resolve_ids",
    "COLLECT_JS",
    "STATE_JS",
    "DM_SCROLL_JS",
    "CHAT_GRACE_SECONDS",
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

# DM chats: once the conversation is open (logged in), start scrolling the
# history even before a video appears - conversations with no video at the
# top must still be walked. Grace period (seconds) before starting anyway.
CHAT_GRACE_SECONDS = 12.0

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


def is_chat_url(url: str) -> bool:
    """True when *url* is an X DM conversation (x.com/i/chat/... or /messages).

    DM pages need the logged-in in-app session and a different harvest
    strategy (videos are recovered from the page's own network traffic,
    not from tweet articles).
    """
    path = urllib.parse.urlsplit(url or "").path.lower()
    return path.startswith("/i/chat") or path == "/messages"


def page_kind(url: str) -> str:
    """Label for a page URL: 'chat', 'search', 'profile' or 'page'."""
    if is_chat_url(url):
        return "chat"
    if is_search_url(url):
        return "search"
    path = urllib.parse.urlsplit(url or "").path.strip("/")
    if path and "/" not in path:
        return "profile"
    return "page"


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
var vids = document.querySelectorAll("video").length;
var login = !!document.querySelector(
  "a[data-testid='loginButton'], input[autocomplete='username']");
var h = document.scrollingElement
  ? document.scrollingElement.scrollHeight
  : document.documentElement.scrollHeight;
return JSON.stringify({ articles: arts, videos: vids, login: login, h: h });
"""

# DM conversation probe: walks the message history UP (older messages load
# on the way back), nudges every mounted video to play so its real CDN URL
# is requested (the network capture records it - DM videos have no tweet
# ids, so traffic is the only data source), and reports the scroll position
# so the Python side can tell when the history top is reached.
DM_SCROLL_JS = r"""
var clicked = 0;
var vids = document.querySelectorAll("video").length;
document.querySelectorAll("video").forEach(function(v){
  try { v.muted = true; if (v.paused) { v.play().catch(function(){}); } clicked++; }
  catch(e) {}
});
document.querySelectorAll(
  "[data-testid='videoComponent'],[data-testid='videoPlayer'],"
  + "[aria-label*='video' i]"
).forEach(function(t){ try { t.click(); clicked++; } catch(e) {} });
var best = null, bestDiff = 0;
document.querySelectorAll("div").forEach(function(d){
  var diff = d.scrollHeight - d.clientHeight;
  if (diff > bestDiff && d.clientHeight > 150){ bestDiff = diff; best = d; }
});
var h = 0, top = 0;
if (best) {
  h = best.scrollHeight; top = best.scrollTop;
  best.scrollTop = Math.max(0, best.scrollTop - 700);
} else {
  window.scrollBy(0, -700);
  h = document.scrollingElement ? document.scrollingElement.scrollHeight : 0;
  top = window.scrollY;
}
return JSON.stringify({ v: vids, clicked: clicked, h: h, top: top });
"""


def resolve_ids(
    ids: list[str],
    progress=None,
    on_items=None,
    stop_event: threading.Event | None = None,
    resolve_fallback=None,
    on_counts=None,
) -> list[core.VideoItem]:
    """Turn collected tweet ids into VideoItems via the syndication engine.

    Shared by both collectors: the in-app webview (Qt build) and the Edge
    fallback. Delegates to core.fetch_videos_batch() for the actual network
    work - same rate-limit handling (cooldown-and-retry passes) as a
    directly-pasted link list gets, instead of the old single-retry-then-
    give-up logic this used to have of its own. Emits *on_items* in chunks
    of :data:`RESOLVE_CHUNK` videos so the GUI can render results while the
    rest resolve.

    *resolve_fallback*, passed straight through to fetch_videos_batch, is
    tried for an id the public API refuses (TombstoneError) - lets the
    authenticated in-app browser recover a video the public endpoint can't
    see. Search results are exactly where this matters most: the scroll
    collector can find a post whose video only a logged-in session can
    reach.

    *on_counts*, if given, is called once with ``(ok, failed)`` link counts
    after the batch finishes, so callers can show a per-link summary.
    """
    progress = progress or (lambda msg: None)
    on_items = on_items or (lambda chunk: None)
    stop_event = stop_event or threading.Event()
    total = len(ids)
    items: list[core.VideoItem] = []
    chunk: list[core.VideoItem] = []
    resolved = 0

    def _on_item(got: list[core.VideoItem]) -> None:
        nonlocal resolved, chunk
        resolved += 1
        items.extend(got)
        chunk.extend(got)
        progress(f"Resolving videos {resolved}/{total}...")
        if len(chunk) >= RESOLVE_CHUNK:
            on_items(list(chunk))
            chunk = []

    def _on_error(_url: str, msg: str) -> None:
        progress(msg)

    # A raw tweet id has no known author handle - "/i/status/<id>" matches
    # the same URL pattern fetch_videos() parses (the username segment is
    # discarded, only the numeric id is used).
    urls = [f"https://x.com/i/status/{tid}" for tid in ids]
    ok, failed = core.fetch_videos_batch(
        urls, on_item=_on_item, on_error=_on_error, stop_event=stop_event,
        resolve_fallback=resolve_fallback,
    )
    if on_counts:
        try:
            on_counts(ok, failed)
        except Exception:
            pass
    if chunk:
        on_items(list(chunk))
    progress(f"Done - {len(items)} videos ready for download")
    return items


class SearchCollector:
    """Open ANY x.com page in Edge, scroll it to the end, resolve videos.

    Parameters
    ----------
    url:
        Canonical page URL (search, profile timeline, /media, ...).
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
        resolve_fallback=None,
    ) -> None:
        self.url = url
        self.progress = progress or (lambda msg: None)
        self.on_items = on_items or (lambda items: None)
        self.stop_event = stop_event or threading.Event()
        self.resolve_fallback = resolve_fallback
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
            "Page fetching opens Edge to load the page - install Edge "
            "(microsoft.com/edge) and try again."
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
            self.progress("Opening the page in Edge...")
            driver = self._create_driver()
            try:
                driver.get(self.url)
            except Exception as exc:
                raise core.XGrabError(
                    f"The page did not load: {exc}"
                ) from exc

            if not self._wait_for_results(driver):
                if self.stop_event.is_set():
                    return self._resolve_all()
                raise core.XGrabError(
                    "No posts appeared in Edge.\n"
                    "Log in to X in the Edge window when it asks, then "
                    "press Fetch videos again."
                )

            # -- auto-scroll to the very end of the feed, resolving videos
            # for each newly-found id on a worker thread as scrolling goes,
            # instead of resolving only after the whole feed is collected --
            resolved: set[str] = set()
            resolve_pool = ThreadPoolExecutor(max_workers=1)
            pending: list = []

            def resolve_new(ids: list[str]) -> None:
                new_ids = [tid for tid in ids if tid not in resolved]
                if not new_ids:
                    return
                resolved.update(new_ids)
                resolve_ids(
                    new_ids,
                    progress=self.progress,
                    on_items=self.on_items,
                    stop_event=self.stop_event,
                    resolve_fallback=self.resolve_fallback,
                )

            self.progress("Scrolling the page - collecting videos...")
            started = time.monotonic()
            last_h = -1
            last_count = -1
            stable = 0
            try:
                while not self.stop_event.is_set():
                    if time.monotonic() - started > HARD_CAP_SECONDS:
                        self.progress("Safety cap reached - using what we have.")
                        break
                    driver.execute_script(
                        "window.scrollBy(0, Math.max(400, "
                        "Math.round(window.innerHeight * 0.9)));"
                    )
                    time.sleep(SCROLL_STEP_SLEEP)
                    before = set(self.found)
                    self._harvest(driver)
                    new_ids = [tid for tid in self.found if tid not in before]
                    if new_ids:
                        pending.append(resolve_pool.submit(resolve_new, new_ids))
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
                        "The page may contain no videos."
                    )
                # resolving already overlapped with scrolling above - wait
                # for the in-flight work, then catch any stragglers
                for fut in pending:
                    fut.result()
                remaining = [
                    tid for tid in self.found if tid not in resolved
                ]
                if remaining:
                    resolve_new(remaining)
                return []
            finally:
                resolve_pool.shutdown(wait=True)
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
            resolve_fallback=self.resolve_fallback,
        )
