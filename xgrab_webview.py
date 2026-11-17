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
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
    )
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
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
