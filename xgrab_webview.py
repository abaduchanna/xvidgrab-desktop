"""XVidGrab Desktop - in-app search collector (embedded Chromium webview).

v1.2.0: search pages no longer open an external Edge window.  The page loads
in a window INSIDE the app (PySide6 QtWebEngine - the same Chromium engine
Edge uses), so when X asks for a login you log in right there in the XVidGrab
window.  No browser windows, no profiles to manage, nothing outside the app.

The session lives in a dedicated persistent profile (~/XVidGrab-WebViewProfile)
- log in once and every later search (and every later app start) reuses it,
exactly like a normal browser remembering you.

Same collection engine as before: auto-scroll the results to the very end,
harvest every video card (extension v1.2.0 DOM rules), then resolve each post
through the public syndication engine into the normal download list.

Developed by www.3SVerse.com - part of the 3SVerse suite.
MIT licensed - see LICENSE.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import xgrab_core as core
import xgrab_search as xsearch

__all__ = [
    "webengine_available",
    "run_search_dialog",
    "clean_user_agent",
    "parse_harvest",
    "merge_harvest",
    "resolve_via_authenticated_browser",
    "shutdown_resolve_view",
    "WEBVIEW_PROFILE_DIR",
]

# Persistent webview profile - log in once, reused by every later run
# (separate from the legacy Edge profile so the two never collide).
WEBVIEW_PROFILE_DIR = os.path.join(
    os.path.expanduser("~"), "XVidGrab-WebViewProfile"
)

# How often the loading phase polls the page (seconds) and how long a
# runJavaScript callback may stall before the watchdog calls it quits.
POLL_INTERVAL = 2.0
STALL_TIMEOUT = 90.0
MAX_LOGIN_RELOADS = 5

# strip the "QtWebEngine/6.8 " token so x.com sees a normal Chromium browser
_UA_QT_TOKEN_RE = re.compile(r"QtWebEngine/[0-9.]+\s*")


# --------------------------------------------------------------------------
# Pure helpers (unit-testable without Qt / without a display)
# --------------------------------------------------------------------------

def clean_user_agent(ua: str) -> str:
    """Remove the ``QtWebEngine/x.y`` token from a user-agent string.

    ``... (KHTML, like Gecko) QtWebEngine/6.8 Chrome/131.0.0.0 Safari/537.36``
    becomes ``... (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36`` - the
    real Chromium version stays, so the page still gets accurate capability
    hints while the embedded fingerprint stops announcing itself.
    """
    if not ua:
        return ua or ""
    return _UA_QT_TOKEN_RE.sub("", ua).strip()


def parse_harvest(raw) -> dict:
    """Parse a runJavaScript / execute_script result into a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def merge_harvest(found: dict, payload: dict) -> tuple[int, int]:
    """Merge one COLLECT_JS payload into *found* (id -> {"user","text"}).

    Returns ``(n, h)`` - cards mounted in this pass and the page height, the
    same contract as ``SearchCollector._harvest``.
    """
    for rec in payload.get("items") or []:
        tid = str(rec.get("id") or "")
        if not tid.isdigit():
            continue
        if tid not in found:
            found[tid] = {
                "user": rec.get("user") or "",
                "text": rec.get("text") or "",
            }
    return int(payload.get("n") or 0), int(payload.get("h") or 0)


def webengine_available() -> bool:
    """True when the embedded browser (QtWebEngine) can be used."""
    return _QT_OK


def iife(js: str) -> str:
    """Wrap *js* in an immediately-invoked function expression.

    ``STATE_JS`` / ``COLLECT_JS`` end in a top-level ``return`` - valid for
    selenium's ``execute_script`` (which wraps scripts in a function body)
    but a SyntaxError for ``QWebEnginePage.runJavaScript`` (a classic
    script).  Wrapping in ``(function(){ ... })()`` keeps the return and
    works everywhere.
    """
    return "(function(){" + (js or "") + "})()"


# The dialog restyles itself with the same palette as the main window, but
# this module cannot import the GUI module (circular import), so the hex
# values live here, mirrored from xvidgrab_desktop_qt.py.
_PALETTE = {
    "BG": "#07060b",
    "SURFACE": "#0d0c14",
    "CARD": "#16141f",
    "BORDER": "#232130",
    "TEXT": "#f5f1e6",
    "AMBER": "#f5c518",
}


def core_hex(name: str) -> str:
    """Palette lookup for the dialog stylesheet (mirror of the main QSS)."""
    return _PALETTE[name]


# --------------------------------------------------------------------------
# Qt section - everything below needs PySide6 + QtWebEngineWidgets, and is
# guarded so the module stays importable (and testable) without them.
# --------------------------------------------------------------------------

try:  # pragma: no cover - exercised implicitly by the import machinery
    from PySide6.QtCore import QEventLoop, QObject, QTimer, QUrl
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
    )
    from PySide6.QtWebEngineCore import (
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineUrlRequestInterceptor,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView

    _QT_OK = True
except Exception:  # noqa: BLE001 - any missing piece disables the webview
    _QT_OK = False


if _QT_OK:

    _shared_profile = None

    def shared_profile():
        """One persistent Chromium profile for the whole app lifetime.

        Parented to the QApplication (not to any dialog) so closing a search
        window never tears the cookie store down mid-write - the login made
        in one search carries over to the next and to the next app start.
        """
        global _shared_profile
        if _shared_profile is None:
            prof = QWebEngineProfile("xvidgrab", QApplication.instance())
            prof.setPersistentStoragePath(WEBVIEW_PROFILE_DIR)
            prof.setCachePath(os.path.join(WEBVIEW_PROFILE_DIR, "Cache"))
            prof.setPersistentCookiesPolicy(
                QWebEngineProfile.ForcePersistentCookies
            )
            prof.setHttpUserAgent(clean_user_agent(prof.httpUserAgent()))
            _shared_profile = prof
        return _shared_profile

    # ----------------------------------------------------------------
    # Authenticated resolve: recover a video the public syndication API
    # refuses (TombstoneError - age-restricted/sensitive/limited) by
    # loading the post in the app's own logged-in browser session and
    # watching for its real video CDN requests, the way a human browsing
    # the page would see them load. The public API has no login at all,
    # so this is the only way this app can ever reach that content.
    # ----------------------------------------------------------------
    _VIDEO_CDN_RE = re.compile(
        r"https://video\.twimg\.com/[^\s\"']+\.(?:mp4|m3u8)(?:\?[^\s\"']*)?"
    )

    # How long the gate/thumbnail click-through polls for, and how often -
    # X's React hydration time varies with network/render speed, so this
    # keeps retrying across the whole window rather than guessing one delay.
    RESOLVE_CLICK_INTERVAL_MS = 350
    RESOLVE_CLICK_MAX_TRIES = 18          # 18 * 350ms = 6.3s of retrying
    RESOLVE_SETTLE_MS = 6500              # Python-side wait >= the JS retry window

    class _VideoCaptureInterceptor(QWebEngineUrlRequestInterceptor):
        """Watches every request on the shared profile, but only records
        the ones whose originating page is the one resolve just navigated
        to - a search dialog's own scroll traffic on the same shared
        profile passes through untouched and unrecorded, since its
        requests carry a different firstPartyUrl.

        Matched by tweet id being present in firstPartyUrl, NOT an exact
        URL match: x.com/i/status/<id> (what we navigate to - the only
        form that works without already knowing the author's handle)
        redirects to the canonical x.com/<user>/status/<id> once it
        resolves, so the page's real, final URL never equals the one we
        set. An exact match would silently record nothing, ever, even
        on a page that loaded and played the video correctly."""

        def __init__(self) -> None:
            super().__init__()
            self.target_tweet_id = ""
            self.found: list[str] = []

        def interceptRequest(self, info) -> None:  # noqa: N802 - Qt override
            try:
                if not self.target_tweet_id:
                    return
                if self.target_tweet_id not in info.firstPartyUrl().toString():
                    return
                url = info.requestUrl().toString()
                if _VIDEO_CDN_RE.match(url):
                    self.found.append(url)
            except Exception:
                pass  # must never raise - that can take the page load down with it

    _capture_interceptor = None

    def _ensure_interceptor():
        global _capture_interceptor
        if _capture_interceptor is None:
            _capture_interceptor = _VideoCaptureInterceptor()
            shared_profile().setUrlRequestInterceptor(_capture_interceptor)
        return _capture_interceptor

    def _video_items_from_urls(tweet_id: str, urls: list[str]) -> list:
        mp4s = [u for u in urls if u.endswith(".mp4") or ".mp4?" in u]
        fmts = []
        for u in mp4s:
            w, h = core._dims_from_url(u)
            fmts.append(
                core.VideoFormat(
                    content_type="video/mp4", width=w, height=h, bitrate=0, url=u
                )
            )
        by_res = {}
        for f in fmts:
            res = (f.width, f.height)
            if res not in by_res:  # first seen at each resolution wins
                by_res[res] = f
        fmts = sorted(
            by_res.values(), key=lambda f: (f.height * f.width), reverse=True
        )
        if not fmts:
            return []
        return [
            core.VideoItem(
                tweet_id=tweet_id,
                index=1,
                total=1,
                uploader="x",
                text="",
                duration_ms=None,
                media_type="video",
                thumbnail_url="",
                formats=fmts,
                webpage_url=f"https://x.com/i/status/{tweet_id}",
            )
        ]

    def _dump_resolve_debug(page, tweet_id: str, loaded: bool, found_urls: list) -> None:
        """Save what the authenticated-browser resolve actually saw when it
        comes back empty - page title, a text snippet, and whatever video
        URLs (if any) the interceptor did catch. Diagnosing this blind a
        second time isn't useful; this gives something concrete to look at
        instead of guessing again."""
        try:
            info = {"title": "", "snippet": ""}
            inner_loop = QEventLoop()

            def on_result(value) -> None:
                if isinstance(value, dict):
                    info["title"] = str(value.get("title") or "")
                    info["snippet"] = str(value.get("snippet") or "")[:600]
                inner_loop.quit()

            def parse_and_call(raw) -> None:
                try:
                    on_result(json.loads(raw) if isinstance(raw, str) else {})
                except Exception:
                    on_result({})

            page.runJavaScript(iife(
                "return JSON.stringify({"
                "  title: document.title || '',"
                "  snippet: (document.body ? document.body.innerText : '').slice(0, 600)"
                "});"
            ), parse_and_call)
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(inner_loop.quit)
            timer.start(2000)
            inner_loop.exec()
            timer.stop()

            debug_dir = os.path.join(core.default_download_dir(), "debug")
            os.makedirs(debug_dir, exist_ok=True)
            path = os.path.join(debug_dir, f"{tweet_id}_authresolve.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"tweet_id: {tweet_id}\n")
                f.write(f"page_loaded: {loaded}\n")
                f.write(f"video_urls_captured: {len(found_urls)}\n")
                for u in found_urls:
                    f.write(f"  - {u}\n")
                f.write(f"page_title: {info['title']}\n")
                f.write(f"page_text_snippet:\n{info['snippet']}\n")
        except Exception:
            pass  # diagnostics must never break the actual resolve

    _resolve_view = None
    _resolve_page = None

    def _ensure_resolve_view():
        """One hidden, off-screen QWebEngineView reused for every resolve,
        instead of a fresh view+page per tweet. Two reasons: (1) repeatedly
        creating/tearing down QWebEngineView/Page instances (each backed by
        its own QtWebEngine child process) is slow across a big batch, and
        (2) a deleteLater()'d view/page can outlive app.exec() returning if
        the event loop stops before the deferred deletion actually runs,
        leaving its QtWebEngine child process orphaned in the background
        after the main window closes. A single long-lived instance, torn
        down explicitly via shutdown_resolve_view() on app exit, avoids
        both.
        """
        global _resolve_view, _resolve_page
        if _resolve_view is None:
            _resolve_page = QWebEnginePage(shared_profile())
            # A bare QWebEnginePage with no attached view has no real
            # viewport - X (like most modern sites) only mounts the actual
            # video player when its element is "visible"
            # (IntersectionObserver), which never fires with no viewport at
            # all. A real, sized QWebEngineView gives it one; moving the
            # view far off-screen before showing it keeps it genuinely
            # invisible to the user while still getting a real native
            # window and a working compositor/viewport underneath.
            _resolve_view = QWebEngineView()
            _resolve_view.setPage(_resolve_page)
            _resolve_view.resize(1000, 1400)
            _resolve_view.move(-32000, -32000)
            # NOT shown here - see resolve_via_authenticated_browser(). A
            # QWidget counts as an open top-level window for Qt's "quit
            # when the last window closes" the moment .show() runs, even
            # positioned off-screen - leaving it shown for the rest of the
            # app's life is exactly what stopped the main window's close
            # from ever actually quitting the app.
        return _resolve_view, _resolve_page

    def shutdown_resolve_view():
        """Explicitly tear down the reused resolve view/page. Call this
        from the app's aboutToQuit (or equivalent shutdown path) - relying
        on Qt/Python object teardown alone at process exit is exactly what
        leaves a QtWebEngine child process running in the background after
        the main window closes."""
        global _resolve_view, _resolve_page
        if _resolve_view is not None:
            try:
                _resolve_view.setPage(None)
                _resolve_view.close()
                _resolve_view.deleteLater()
            except Exception:
                pass
            _resolve_view = None
        if _resolve_page is not None:
            try:
                _resolve_page.deleteLater()
            except Exception:
                pass
            _resolve_page = None

    def resolve_via_authenticated_browser(tweet_id: str, timeout: float = 8.0):
        """Load the post in the logged-in browser session and watch its
        real network traffic for video CDN URLs. MUST be called from the
        GUI thread - QtWebEngine requires it; a background worker reaches
        this through a Qt signal/blocking-queued-connection bridge, same
        pattern as every other GUI-thread handoff in this app. Returns
        list[core.VideoItem] (possibly empty) or None if the page itself
        never loaded (offline, blocked, etc).
        """
        interceptor = _ensure_interceptor()
        view, page = _ensure_resolve_view()
        # Only open for the duration of this one resolve - see
        # _ensure_resolve_view()'s note on why leaving it permanently shown
        # blocks the app from ever quitting when the main window closes.
        view.show()
        url = f"https://x.com/i/status/{tweet_id}"
        interceptor.target_tweet_id = tweet_id
        interceptor.found = []

        state = {"loaded": False}
        loop = QEventLoop()

        def on_load(ok) -> None:
            state["loaded"] = True
            loop.quit()

        page.loadFinished.connect(on_load)
        page.setUrl(QUrl(url))

        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(int(timeout * 1000))
        loop.exec()
        timer.stop()
        try:
            page.loadFinished.disconnect(on_load)
        except Exception:
            pass  # the shared page is reused - never leave a stale
            # per-call connection attached, or every future resolve's
            # loadFinished would also re-fire every earlier call's closure

        if state["loaded"]:
            # Two separate things can block the real <video> from ever
            # mounting, confirmed from real debug_authresolve.txt dumps:
            # (1) a sensitive-content interstitial ("Content warning:
            # Nudity... Show" - the button says "Show", not "View") that
            # must be clicked before anything else renders; (2) even with
            # no warning at all, the player itself is a click-to-mount
            # thumbnail/poster - X doesn't create the <video> element until
            # that's clicked, so just calling .play() on a <video> that
            # doesn't exist yet is a no-op.
            #
            # A fixed-delay guess (click at 0ms, retry at 700ms) was STILL
            # too early in real testing - X's React hydration of the tweet
            # (and the gate/player inside it) can take longer than that,
            # variably, depending on network/render speed. The debug dump
            # taken at the END of the full settle window DID show the gate
            # text correctly, proving the content eventually renders - the
            # fixed delays just didn't wait long enough. Poll instead: keep
            # retrying both clicks every 350ms for the whole settle window,
            # so whenever hydration actually finishes, the very next tick
            # catches it - no more guessing a single "right" delay.
            try:
                page.runJavaScript(iife(
                    "function clickByText(re){"
                    "  var nodes = document.querySelectorAll(\"div[role='button'],span,button\");"
                    "  for (var i = 0; i < nodes.length; i++) {"
                    "    var t = (nodes[i].innerText || '').trim();"
                    "    if (re.test(t)) { nodes[i].click(); return true; }"
                    "  }"
                    "  return false;"
                    "}"
                    "function clickVideoArea(){"
                    "  var el = document.querySelector("
                    "    \"[data-testid='videoComponent'],[data-testid='videoPlayer'],\"+"
                    "    \"[data-testid='playButton'],[aria-label='Play'],[aria-label='Play Video']\");"
                    "  if (el) { el.click(); return true; }"
                    "  return false;"
                    "}"
                    "function nudgePlay(){"
                    "  var v = document.querySelector('video');"
                    "  if (v) { v.muted = true; v.play().catch(function(){}); return !!v; }"
                    "  return false;"
                    "}"
                    f"var tries = 0, maxTries = {RESOLVE_CLICK_MAX_TRIES};"
                    "var timer = setInterval(function(){"
                    "  tries++;"
                    "  clickByText(/^(show|view)$/i);"
                    "  clickVideoArea();"
                    "  nudgePlay();"
                    "  if (tries >= maxTries) clearInterval(timer);"
                    f"}}, {RESOLVE_CLICK_INTERVAL_MS});"
                ))
            except Exception:
                pass
            settle = QEventLoop()
            settle_timer = QTimer()
            settle_timer.setSingleShot(True)
            settle_timer.timeout.connect(settle.quit)
            settle_timer.start(RESOLVE_SETTLE_MS)
            settle.exec()

        interceptor.target_tweet_id = ""  # stop recording, keep the view/page
        # for the next resolve - shutdown_resolve_view() tears it down for
        # real, once, at app exit (see its docstring for why).
        urls = list(dict.fromkeys(interceptor.found))  # de-dup, keep order
        items = _video_items_from_urls(tweet_id, urls) if state["loaded"] else []
        if not items:
            _dump_resolve_debug(page, tweet_id, state["loaded"], urls)
        view.hide()  # closed between resolves - see the .show() note above
        if not state["loaded"]:
            return None
        return items

    class SearchWebViewDialog(QDialog):
        """In-app browser window: load, log in (here!), scroll, harvest."""

        def __init__(self, parent, url: str, stop_event=None, on_new_ids=None) -> None:
            super().__init__(parent)
            self.setWindowTitle(f"{core.APP_NAME} - Search")
            self.resize(1120, 800)
            self.setModal(True)
            self.stop_event = stop_event or threading.Event()
            self.search_url = url
            # called with a list of newly-found tweet ids after every scroll
            # pass, so a worker thread can start resolving videos while
            # scrolling is still going instead of waiting for it to finish
            self.on_new_ids = on_new_ids

            # collector state
            self.found: dict[str, dict] = {}
            self.stopped = False
            self.error: str | None = None
            self._phase = "loading"          # loading -> scrolling -> done
            self._told_login = False
            self._login_reloads = 0
            self._load_deadline = (
                time.monotonic() + xsearch.LOGIN_WAIT_SECONDS
            )
            self._scroll_started = 0.0
            self._last_h = -1
            self._last_count = -1
            self._stable = 0
            self._closed = False
            self._last_cb = time.monotonic()
            self.status_hook = lambda msg: None   # set by run_search_dialog

            self.setStyleSheet(
                f"QDialog {{ background: {core_hex('BG')}; }}"
                f"QLabel {{ color: {core_hex('TEXT')}; background: transparent; }}"
                f"QPushButton {{ background: {core_hex('SURFACE')};"
                f" color: {core_hex('TEXT')}; border: 1px solid"
                f" {core_hex('BORDER')}; border-radius: 6px; padding: 6px 14px;"
                " font-weight: bold; }}"
                f"QPushButton:hover {{ background: {core_hex('CARD')}; }}"
            )

            lay = QVBoxLayout(self)
            lay.setContentsMargins(12, 10, 12, 10)
            lay.setSpacing(6)

            head = QHBoxLayout()
            self.status_lbl = QLabel("Loading the search page inside the app...")
            head.addWidget(self.status_lbl, 1)
            self.stop_btn = QPushButton("Stop collecting")
            self.stop_btn.clicked.connect(self._stop)
            head.addWidget(self.stop_btn)
            lay.addLayout(head)

            self.banner = QLabel(
                "X login required - log in RIGHT HERE in this window "
                "(one time only).\nThe collector keeps watching and "
                "continues automatically once you are in."
            )
            self.banner.setWordWrap(True)
            self.banner.setStyleSheet(
                f"background: {core_hex('SURFACE')}; color: {core_hex('AMBER')};"
                " border: 1px solid #6b5a17; border-radius: 6px; padding: 8px;"
                " font-weight: bold;"
            )
            self.banner.setVisible(False)
            lay.addWidget(self.banner)

            self.view = QWebEngineView()
            lay.addWidget(self.view, 1)

            self.profile = shared_profile()
            self.page = QWebEnginePage(self.profile, self.view)
            self.view.setPage(self.page)
            try:
                self.page.setAudioMuted(True)   # no surprise autoplay sound
            except Exception:
                pass
            self.view.renderProcessTerminated.connect(self._on_render_crash)
            self.view.urlChanged.connect(lambda _u: self._ping())

            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._tick)

            self._watchdog = QTimer(self)
            self._watchdog.timeout.connect(self._on_watchdog)

            self.view.load(QUrl(url))
            self._timer.start(int(POLL_INTERVAL * 1000))
            self._watchdog.start(10_000)

        # ------------------------------------------------------------ utils
        def _ping(self) -> None:
            self._last_cb = time.monotonic()

        def _set_status(self, msg: str) -> None:
            self.status_lbl.setText(msg)
            try:
                self.status_hook(msg)
            except Exception:
                pass

        def _on_watchdog(self) -> None:
            if self._closed:
                return
            if time.monotonic() - self._last_cb > STALL_TIMEOUT:
                self.error = (
                    "The embedded browser stopped responding.\n"
                    "Close this window and press Fetch videos again."
                )
                self._finish()

        def _on_render_crash(self, *_args) -> None:
            if self._closed:
                return
            self.error = (
                "The embedded browser crashed while loading the page.\n"
                "Press Fetch videos again - if it keeps happening, restart "
                "the app."
            )
            self._finish()

        # ------------------------------------------------------ state machine
        def _tick(self) -> None:
            if self._closed:
                return
            if self.stop_event.is_set():
                self._finish(stopped=True)
                return
            if self._phase == "loading":
                if time.monotonic() > self._load_deadline:
                    self.error = (
                        "No search results appeared.\n"
                        "If the window asked you to log in, finish logging in "
                        "and press Fetch videos again."
                    )
                    self._finish()
                    return
                self.page.runJavaScript(iife(xsearch.STATE_JS), self._on_state)
            elif self._phase == "scrolling":
                if (
                    time.monotonic() - self._scroll_started
                    > xsearch.HARD_CAP_SECONDS
                ):
                    self._set_status("Safety cap reached - using what we have.")
                    self._finish()
                    return
                self.page.runJavaScript(iife(xsearch.COLLECT_JS), self._on_collect)

        def _on_state(self, raw) -> None:
            if self._closed:
                return
            self._ping()
            state = parse_harvest(raw)
            arts = int(state.get("articles") or 0)
            login = bool(state.get("login"))
            url = self.view.url().toString()
            on_search = any(
                p in url for p in ("/search", "/hashtag", "/explore")
            )

            if arts > 0 and on_search:
                # goal state reached - start the scroll loop
                self.banner.setVisible(False)
                self._phase = "scrolling"
                self._scroll_started = time.monotonic()
                self._set_status(
                    "Scrolling the page - collecting videos..."
                )
                self._timer.start(300)
                return

            if login:
                if not self._told_login:
                    self._told_login = True
                    self.banner.setVisible(True)
            elif (
                self._told_login
                and not login
                and arts > 0
                and not on_search
                and self._login_reloads < MAX_LOGIN_RELOADS
            ):
                # logged in but X bounced us to Home - come back to the search
                self._login_reloads += 1
                self.view.load(QUrl(self.search_url))
                self._timer.start(int(POLL_INTERVAL * 1000))
                return

            self._timer.start(int(POLL_INTERVAL * 1000))

        def _on_collect(self, raw) -> None:
            if self._closed or self._phase != "scrolling":
                return
            self._ping()
            before = set(self.found)
            data = parse_harvest(raw)
            _n, h = merge_harvest(self.found, data)
            count = len(self.found)
            if self.on_new_ids:
                new_ids = [tid for tid in self.found if tid not in before]
                if new_ids:
                    try:
                        self.on_new_ids(new_ids)
                    except Exception:
                        pass
            self._set_status(
                f"Scrolling... {count} video post"
                f"{'s' if count != 1 else ''} found"
            )
            self.page.runJavaScript(
                "window.scrollBy(0, Math.max(400, "
                "Math.round(window.innerHeight * 0.9)));",
                lambda _r=None: self._after_scroll(h),
            )

        def _after_scroll(self, h: int) -> None:
            if self._closed:
                return
            self._ping()
            if h == self._last_h and len(self.found) == self._last_count:
                self._stable += 1
            else:
                self._stable = 0
            self._last_h = h
            self._last_count = len(self.found)
            if self._stable >= xsearch.STABLE_ROUNDS:
                self._finish()
                return
            self._timer.start(int(xsearch.SCROLL_STEP_SLEEP * 1000))

        # ------------------------------------------------------------ finish
        def _stop(self) -> None:
            self.stop_event.set()
            self._finish(stopped=True)

        def _finish(self, stopped: bool = False) -> None:
            if self._closed:
                return
            self._closed = True
            self.stopped = stopped or self.stop_event.is_set()
            self._watchdog.stop()
            self._timer.stop()
            if self.found and not self.error:
                self._set_status(
                    f"Collected {len(self.found)} video post"
                    f"{'s' if len(self.found) != 1 else ''} - "
                    "resolving videos..."
                )
            try:
                self.page.runJavaScript(
                    "window.scrollTo(0, 0);", lambda _r=None: None
                )
            except Exception:
                pass
            self.accept()

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
            # the X button = Stop (keeps whatever was collected so far)
            self.stop_event.set()
            self.stopped = True
            self._closed = True
            self._watchdog.stop()
            self._timer.stop()
            super().closeEvent(event)

    def run_search_dialog(
        parent, url: str, progress=None, stop_event=None, on_new_ids=None
    ):
        """Open the in-app search window and return the harvest result.

        Returns ``{"found": {id: {"user","text"}}, "stopped": bool,
        "error": str | None}``.  Blocks (modal) until the page is fully
        scrolled or the user stops / closes the window; must be called from
        the GUI thread.

        *on_new_ids*, if given, is called with each batch of newly-found
        tweet ids as scrolling happens (same GUI thread) - lets the caller
        start resolving videos in a background thread while the dialog is
        still scrolling, instead of waiting for it to close.
        """
        dlg = SearchWebViewDialog(
            parent, url, stop_event=stop_event, on_new_ids=on_new_ids
        )
        if progress:
            dlg.status_hook = progress
        dlg.exec()
        return {
            "found": dict(dlg.found),
            "stopped": bool(dlg.stopped),
            "error": dlg.error,
        }

else:  # no Qt / no QtWebEngine - keep the module importable anyway

    def run_search_dialog(*_args, **_kwargs):  # pragma: no cover
        raise RuntimeError(
            "The in-app browser (PySide6 QtWebEngine) is not available in "
            "this build."
        )

    def resolve_via_authenticated_browser(*_args, **_kwargs):  # pragma: no cover
        return None

    def shutdown_resolve_view(*_args, **_kwargs):  # pragma: no cover
        pass
