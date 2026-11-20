#!/usr/bin/env python3
"""XVidGrab Desktop - PySide6 (Qt) GUI.

Paste an X (Twitter) post link, see every video on the page with
thumbnails, pick the ones you want (or Select All) and download them at
the highest available resolution. List view and grid view included.

Run:  python xvidgrab_desktop_qt.py

Developed by www.3SVerse.com - part of the 3SVerse suite. MIT licensed.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image
from PySide6.QtCore import QMetaObject, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import xgrab_core as core
import xgrab_search as xsearch
import xgrab_webview as xweb

BG = "#07060b"
SURFACE = "#0d0c14"
CARD = "#0d0c14"
BORDER = "#232130"
TEXT = "#f5f1e6"
MUTED = "#9b97b3"
ACCENT = "#6ee7ef"
ACCENT_HOVER = "#9deff5"
MAGENTA = "#e44bd7"
GREEN = "#c7ef70"
RED = "#ef6a52"
AMBER = "#f5c518"
THUMB_BG = "#12101a"

GRID_CARD_W = 200
GRID_THUMB_H = 112
LIST_THUMB_W = 160
LIST_THUMB_H = 90
THUMB_POOL = 6
DL_POOL = 3

QSS = f"""
QMainWindow, QWidget {{ background: {BG}; color: {TEXT};
    font-family: 'Segoe UI'; font-size: 10pt; }}
QLabel {{ background: transparent; color: {TEXT}; }}
QLineEdit {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 8px 10px; font-size: 11pt;
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QPushButton {{
    background: {SURFACE}; color: {TEXT}; border: none; border-radius: 6px;
    padding: 7px 14px; font-weight: bold;
}}
QPushButton:hover {{ background: {CARD}; }}
QPushButton:disabled {{ color: {MUTED}; background: {SURFACE}; }}
QPushButton#primary {{ background: {ACCENT}; color: {BG}; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: {BORDER}; color: {MUTED}; }}
QPushButton#active {{ background: {ACCENT}; color: {BG}; }}
QComboBox {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 5px 8px;
}}
QComboBox QAbstractItemView {{ background: {SURFACE}; color: {TEXT};
    selection-background-color: {ACCENT}; }}
QCheckBox {{ background: transparent; color: {TEXT}; }}
QProgressBar {{
    background: {SURFACE}; border: none; border-radius: 4px;
    text-align: center; color: {TEXT}; min-height: 10px; max-height: 10px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QScrollArea {{ border: none; background: {BG}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px;
    min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER};
    border-radius: 8px; }}
"""


def _placeholder_pixmap(w: int, h: int) -> QPixmap:
    img = Image.new("RGB", (w * 2, h * 2), THUMB_BG)
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    cx, cy = img.size
    r = cx // 12
    d.polygon(
        [(cx / 2 - r * 0.5, cy / 2 - r), (cx / 2 - r * 0.5, cy / 2 + r),
         (cx / 2 + r, cy / 2)],
        fill="#2A3A52",
    )
    img = img.resize((w, h), Image.LANCZOS)
    pm = QPixmap()
    pm.loadFromData(_pil_bytes(img))
    return pm


def _pil_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


class MultiPagesDialog(QDialog):
    """'Multiple pages' - paste one or more links and add them all to the
    running fetch list. Non-modal on purpose: the fetch keeps going (and
    stays visible) while more links are pasted here."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{core.APP_NAME} - Multiple pages")
        self.setMinimumSize(580, 430)
        self.setModal(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        tip = QLabel(
            "One link per line - ANY x.com link works:\n"
            "post links, profiles (x.com/Kazimirol2), /media, search pages,\n"
            "DM chat links (x.com/i/chat/...) or just @username.\n\n"
            "Press 'Add to fetch list' and they join the already-running "
            "fetch."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        lay.addWidget(tip)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(
            "https://x.com/Kazimirol2\n"
            "https://x.com/search?q=videos\n"
            "https://x.com/user/status/123..."
        )
        lay.addWidget(self.text, 1)

        btns = QHBoxLayout()
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        self.count_lbl.setMinimumHeight(18)
        btns.addWidget(self.count_lbl, 1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btns.addWidget(close_btn)
        self.add_btn = QPushButton("Add to fetch list")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self._submit)
        btns.addWidget(self.add_btn)
        lay.addLayout(btns)

        self.text.textChanged.connect(self._update_count)
        self._update_count()

    def _update_count(self, *_args) -> None:
        try:
            s, p = core.classify_links(self.text.toPlainText())
            n = len(s) + len(p)
            self.count_lbl.setText(
                f"{n} link{'s' if n != 1 else ''} detected"
                if n else ""
            )
        except Exception:
            pass

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Ctrl+Enter = submit from anywhere in the dialog
        if (
            event.modifiers() & Qt.ControlModifier
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
        ):
            self._submit()
            return
        super().keyPressEvent(event)

    def _submit(self) -> None:
        text = self.text.toPlainText().strip()
        if not text:
            return
        win = self.parent()
        added = 0
        try:
            added = win.add_links(text)
        except Exception:
            added = 0
        if added:
            self.text.clear()
            self.count_lbl.setText(
                f"{added} link{'s' if added != 1 else ''} added"
            )


class Controller(QObject):
    """Thread-safe bridge: worker threads emit, the main thread connects."""

    fetchStarted = Signal(str)                    # status text
    metaReady = Signal(list)                      # VideoItem list
    fetchError = Signal(str)                      # friendly error
    fetchDone = Signal()
    thumbReady = Signal(tuple, bytes)             # key, image bytes
    itemProgress = Signal(tuple, int, object)     # key, downloaded, total
    itemDone = Signal(tuple, str)                 # key, path
    itemFail = Signal(tuple, str)                 # key, error
    allDone = Signal()
    pageDone = Signal(object)                     # Edge page-collection result
    sessionCheck = Signal()                       # resolver idle -> GUI re-check

    # cross-thread bridge for the authenticated-browser resolve fallback -
    # QtWebEngine must run on the GUI thread, but batch-resolve runs on a
    # worker thread. Args/result go through instance attributes (not Qt's
    # argument marshaling) - same pattern already used elsewhere in this
    # app for a worker thread calling back into the GUI thread.
    _browser_resolve_tweet_id = ""
    _browser_resolve_result = None

    # same bridge for the browser-based thumbnail fallback (direct fetch
    # blocked -> route the image through the embedded browser session)
    _thumb_browser_url = ""
    _thumb_browser_result = None

    @Slot()
    def _resolveViaBrowserSlot(self) -> None:
        try:
            self._browser_resolve_result = xweb.resolve_via_authenticated_browser(
                self._browser_resolve_tweet_id
            )
        except Exception:
            self._browser_resolve_result = None

    def resolve_via_browser_sync(self, tweet_id: str):
        """Callable from any thread. Blocks the CALLING thread (not the
        GUI thread) until the authenticated browser has tried to resolve
        *tweet_id*. Returns list[core.VideoItem] or None."""
        if not xweb.webengine_available():
            return None
        self._browser_resolve_tweet_id = tweet_id
        self._browser_resolve_result = None
        QMetaObject.invokeMethod(
            self, "_resolveViaBrowserSlot", Qt.BlockingQueuedConnection
        )
        return self._browser_resolve_result

    @Slot()
    def _thumbViaBrowserSlot(self) -> None:
        try:
            self._thumb_browser_result = xweb.fetch_bytes_via_browser(
                self._thumb_browser_url
            )
        except Exception:
            self._thumb_browser_result = None

    def fetch_thumb_via_browser_sync(self, url: str):
        """Callable from any thread. Blocks the CALLING thread until the
        embedded browser has tried to load the image at *url*. Returns
        image bytes or None."""
        if not xweb.webengine_available():
            return None
        self._thumb_browser_url = url
        self._thumb_browser_result = None
        QMetaObject.invokeMethod(
            self, "_thumbViaBrowserSlot", Qt.BlockingQueuedConnection
        )
        return self._thumb_browser_result


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(core.APP_NAME)
        self.setMinimumSize(960, 620)
        try:
            from PySide6.QtGui import QIcon

            self.setWindowIcon(QIcon(core.resource_path("icons/icon-128.png")))
        except Exception:
            pass

        cfg = core.load_config()
        self.output_dir = cfg.get("output_dir") or core.default_download_dir()
        self.view = cfg.get("view") if cfg.get("view") in ("grid", "list") else "grid"

        self.items: list[core.VideoItem] = []
        self.item_map: dict[tuple, core.VideoItem] = {}
        self.cards: dict[tuple, dict] = {}  # key -> widget refs
        self.pixmaps: dict[tuple, QPixmap] = {}
        self.status_state: dict[tuple, tuple[str, str]] = {}

        self.ctl = Controller()
        self.cancel_event = threading.Event()
        self.fetch_stop = threading.Event()
        self.fetching = False
        self.downloading = False
        self._dl_done = self._dl_ok = self._dl_fail = self._dl_total = 0

        # fetch-session engine state (page queue + unified resolver)
        self._session_open = False
        self._session_sources = 0        # page links not yet collected
        self._session_total = 0          # all sources of this session
        self._page_queue: list[str] = []  # pending page URLs
        self._page_dialog = None          # active non-modal collection window
        self._edge_page_running = False
        self.id_queue = None              # tweet ids waiting to resolve
        self._resolve_thread = None
        self._resolve_thread_queue = None  # that worker's own queue
        self._seen_ids: set[str] = set()
        self._ids_ok = 0
        self._ids_fail = 0
        self._thumb_requested: set = set()
        self._multi_dlg = None            # Multiple pages dialog (kept open)
        self._thumb_browser_failures = 0

        self._build()
        self._connect()
        self.set_view(self.view)
        QTimer.singleShot(10, self._reflow_if_needed)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 10)
        root.setSpacing(6)

        # Header
        head = QHBoxLayout()
        try:
            logo = QLabel()
            pm = QPixmap(core.resource_path("icons/icon-128.png")).scaled(
                34, 34, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            logo.setPixmap(pm)
            head.addWidget(logo)
        except Exception:
            pass
        hleft = QVBoxLayout()
        hleft.setSpacing(0)
        name = QLabel(core.APP_NAME)
        f = name.font()
        f.setPointSize(13)
        f.setBold(True)
        name.setFont(f)
        sub = QLabel("Paste ANY x.com link - post, profile, /media, search or DM chat - pages open inside the app, browser-free")
        sub.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        hleft.addWidget(name)
        hleft.addWidget(sub)
        head.addLayout(hleft)
        head.addStretch(1)
        ver = QLabel(f"v{core.__version__}")
        ver.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        head.addWidget(ver, alignment=Qt.AlignTop)
        root.addLayout(head)

        # URL row
        urlrow = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "https://x.com/user/status/123... - or a profile, /media, search or chat link "
            "   (several links in one paste are fine)"
        )
        self.url_edit.returnPressed.connect(self.start_fetch)
        urlrow.addWidget(self.url_edit, 1)
        self.multi_btn = QPushButton("Multiple pages")
        self.multi_btn.clicked.connect(self.open_multi_dialog)
        urlrow.addWidget(self.multi_btn)
        self.fetch_btn = QPushButton("Fetch videos")
        self.fetch_btn.setObjectName("primary")
        self.fetch_btn.clicked.connect(self.start_fetch)
        urlrow.addWidget(self.fetch_btn)
        root.addLayout(urlrow)

        # Toolbar
        bar = QHBoxLayout()
        self.grid_btn = QPushButton("Grid")
        self.grid_btn.clicked.connect(lambda: self.set_view("grid"))
        self.list_btn = QPushButton("List")
        self.list_btn.clicked.connect(lambda: self.set_view("list"))
        sel_all = QPushButton("Select all")
        sel_all.clicked.connect(self.select_all)
        sel_none = QPushButton("Select none")
        sel_none.clicked.connect(self.select_none)
        self.counter_lbl = QLabel("0 of 0 selected")
        self.counter_lbl.setStyleSheet(f"color: {MUTED};")
        for w in (self.grid_btn, self.list_btn, sel_all, sel_none, self.counter_lbl):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(QLabel("Quality:"))
        self.quality_box = QComboBox()
        self.quality_box.addItem("Best available")
        self.quality_box.setMinimumWidth(120)
        bar.addWidget(self.quality_box)
        folder_btn = QPushButton("Folder...")
        folder_btn.clicked.connect(self.choose_folder)
        bar.addWidget(folder_btn)
        self.folder_lbl = QLabel(self._short_dir())
        self.folder_lbl.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        bar.addWidget(self.folder_lbl)
        root.addLayout(bar)

        # Scroll results
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self._outer_layout = QVBoxLayout(self.scroll_content)
        self._outer_layout.setContentsMargins(0, 0, 8, 0)
        self._outer_layout.setSpacing(6)
        self._outer_layout.addStretch(1)
        self.scroll.setWidget(self.scroll_content)
        root.addWidget(self.scroll, 1)

        self.empty_lbl = QLabel(
            "Paste any x.com link above and press Fetch videos\n\n"
            "A single post, a whole profile, /media, a search page or a chat -\n"
            "every video on that page shows up here with a thumbnail\n\n"
            "Use 'Multiple pages' to add many links at once"
        )
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.empty_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11pt;")
        self._outer_layout.insertWidget(0, self.empty_lbl, alignment=Qt.AlignCenter)

        # Bottom bar
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.status_lbl = QLabel("Ready.")
        self.status_lbl.setStyleSheet(f"color: {MUTED};")
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.status_lbl)
        self.open_btn = QPushButton("Open folder")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_folder)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_download)
        self.download_btn = QPushButton("Download selected")
        self.download_btn.setObjectName("primary")
        self.download_btn.clicked.connect(self.start_download)
        for w in (self.download_btn, self.cancel_btn, self.open_btn):
            bottom.addWidget(w)
        root.addLayout(bottom)

        # 3SVerse brand footer (same standard as License Studio / WiFi
        # Transfer): developed-by centered, version on the right.
        footer = QWidget()
        footer.setStyleSheet(f"background: {SURFACE}; border-top: 1px solid {BORDER};")
        footer.setFixedHeight(28)
        flay = QHBoxLayout(footer)
        flay.setContentsMargins(14, 0, 14, 0)
        flabel = QLabel("DEVELOPED BY WWW.3SVERSE.COM")
        flabel.setStyleSheet(f"color: {MUTED}; font-size: 8pt; font-weight: 700; border: none;")
        fver = QLabel(f"v{core.__version__}")
        fver.setStyleSheet(f"color: {MUTED}; font-size: 8pt; border: none;")
        flay.addStretch(1)
        flay.addWidget(flabel)
        flay.addStretch(1)
        flay.addWidget(fver)
        root.addWidget(footer)

    def _connect(self) -> None:
        self.ctl.fetchStarted.connect(self._on_fetch_started)
        self.ctl.metaReady.connect(self._on_meta)
        self.ctl.fetchError.connect(self._on_fetch_error)
        self.ctl.fetchDone.connect(self._on_fetch_done)
        self.ctl.thumbReady.connect(self._on_thumb)
        self.ctl.itemProgress.connect(self._on_progress)
        self.ctl.itemDone.connect(self._on_item_done)
        self.ctl.itemFail.connect(self._on_item_fail)
        self.ctl.allDone.connect(self._on_all_done)
        self.ctl.pageDone.connect(self._on_page_done)
        self.ctl.sessionCheck.connect(self._on_session_check)

    # -------------------------------------------------------------- fetch
    def _expand_input(self, text: str) -> str:
        """Expand t.co short links so every wrapped link is classifiable."""
        if core._TCO_RE.search(text):
            try:
                return core.expand_tco_links(text)
            except Exception:
                return text
        return text

    def start_fetch(self) -> None:
        # clicking while a fetch session is running stops it (keeps results
        # already found) - a large batch's cooldown-and-retry passes can
        # take a while, so there has to be a way out.
        if self._session_open or self.fetching:
            self.fetch_stop.set()
            self.set_status("Stopping - please wait...", AMBER)
            return
        if self.downloading:
            return
        text = self.url_edit.text().strip()
        status_urls, page_urls = core.classify_links(self._expand_input(text))
        if not status_urls and not page_urls:
            QMessageBox.warning(
                self,
                core.APP_NAME,
                "No x.com link found in the input.\n\n"
                "Paste ANY x.com link - a post (x.com/user/status/123), a\n"
                "profile (x.com/Kazimirol2), /media, a search page or a DM\n"
                "chat link - and every video on that page is fetched.\n\n"
                "Use 'Multiple pages' to add several links at once.",
            )
            return
        self.fetching = True
        self._reset_results()
        self._start_session(status_urls, page_urls)

    def add_links(self, text: str, reset: bool = False) -> int:
        """Classify *text* and add its links to the fetch session.

        Called by the Multiple pages dialog (and available for future
        callers). When a session is already running the new links simply
        join it - page links queue up behind the current one, post links
        resolve straight away alongside whatever is already running.
        Returns the number of links accepted.
        """
        status_urls, page_urls = core.classify_links(self._expand_input(text))
        count = len(status_urls) + len(page_urls)
        if not count:
            self.set_status(
                "No x.com links found in the pasted text.", AMBER
            )
            return 0
        if reset or not self._session_open:
            if reset and not self.downloading:
                self.fetching = True
                self._reset_results()
            elif not self._session_open:
                self.fetching = True
            self._start_session(status_urls, page_urls)
        else:
            # join the already-running fetch list
            self._session_sources += len(page_urls)
            self._session_total += count
            for u in status_urls:
                m = core._STATUS_RE.search(u)
                if m and self.id_queue is not None:
                    self.id_queue.put(m.group(2))
            self._page_queue.extend(page_urls)
            self.set_status(
                f"{count} link{'s' if count != 1 else ''} added to the "
                "running fetch list", ACCENT,
            )
            QTimer.singleShot(0, self._pump_pages)
        return count

    def _reset_results(self) -> None:
        """Reset previous results (a fresh fetch from the main input)."""
        self.items = []
        self.item_map = {}
        self.cards = {}
        self.pixmaps = {}
        self._thumb_requested = set()
        self.status_state = {}
        self._clear_results()
        self._update_counter()

    # ------------------------------------------------------ fetch session
    def _start_session(self, status_urls: list, page_urls: list) -> None:
        """Open a fetch session: one resolver worker + a page queue.

        Post links are turned into tweet ids and resolved through the
        public syndication endpoint. Every page link (profiles, /media,
        searches, chats) opens the in-app browser window one after another
        - each new link added mid-run joins the same queues, which is what
        'Multiple pages' builds on.
        """
        self.fetch_stop.clear()
        self._session_open = True
        self._session_sources = len(page_urls)
        self._session_total = len(status_urls) + len(page_urls)
        self._ids_ok = 0
        self._ids_fail = 0
        self._seen_ids = set()
        self.id_queue = queue.Queue()
        self._page_queue = list(page_urls)
        # if a previous session's resolver thread is still unwinding, tell
        # it to stop now (its sentinel) so it never lingers spinning
        if (
            self._resolve_thread is not None
            and self._resolve_thread.is_alive()
            and isinstance(self._resolve_thread_queue, queue.Queue)
        ):
            try:
                self._resolve_thread_queue.put(None)
            except Exception:
                pass
        self._resolve_thread_queue = self.id_queue

        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Stop")

        if status_urls:
            self.set_status(
                f"Fetching {self._session_total} "
                f"link{'s' if self._session_total != 1 else ''}...", MUTED
            )
            for u in status_urls:
                m = core._STATUS_RE.search(u)
                if m:
                    self.id_queue.put(m.group(2))
        self._resolve_thread = threading.Thread(
            target=self._resolve_worker_loop, args=(self.id_queue,),
            daemon=True,
        )
        self._resolve_thread.start()
        self._pump_pages()

    def _resolve_worker_loop(self, id_queue: "queue.Queue") -> None:
        """Drain tweet ids off *id_queue* and resolve them as they arrive.

        Runs for the whole life of a session: post links and every video
        post harvested from a page all flow through this one worker, so
        syndication requests stay paced and results land live. Exits when
        a ``None`` sentinel arrives (pushed by _maybe_finish_session once
        every source is done) and the queue is drained.
        """
        done = False
        while True:
            batch: list[str] = []
            try:
                item = id_queue.get(timeout=0.5)
            except queue.Empty:
                item = "__timeout__"
            if item is None:
                done = True
            elif item != "__timeout__":
                batch.append(item)
            # drain whatever else is already queued without blocking, so a
            # burst of new ids from one scroll pass resolves as one batch
            while True:
                try:
                    nxt = id_queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    done = True
                else:
                    batch.append(nxt)
            batch = [tid for tid in batch if tid not in self._seen_ids]
            if batch:
                self._seen_ids.update(batch)
                try:
                    xsearch.resolve_ids(
                        batch,
                        progress=lambda m: self.ctl.fetchStarted.emit(m),
                        on_items=lambda chunk: self.ctl.metaReady.emit(chunk),
                        stop_event=self.fetch_stop,
                        resolve_fallback=self.ctl.resolve_via_browser_sync,
                        on_counts=self._on_resolve_counts,
                    )
                except Exception as exc:  # defensive
                    self.ctl.fetchError.emit(f"Unexpected error: {exc}")
            if done and id_queue.empty():
                break
            if not done and id_queue.empty():
                # queue fully drained but sources may still be closing -
                # nudge the GUI thread to re-check session completion
                self.ctl.sessionCheck.emit()
        # only the CURRENT session's worker may close it down (a stopped
        # session can be followed by a new one while this thread unwinds)
        if self.id_queue is id_queue:
            self._session_open = False
            self.fetching = False
            self.ctl.fetchDone.emit()

    def _on_resolve_counts(self, ok: int, failed: int) -> None:
        self._ids_ok += ok
        self._ids_fail += failed

    def _on_session_check(self) -> None:
        self._maybe_finish_session()

    def _maybe_finish_session(self) -> None:
        """End the session once every source is closed and drained."""
        if not self._session_open:
            return
        if (
            self._session_sources == 0
            and not self._page_queue
            and self._page_dialog is None
            and not self._edge_page_running
            and self.id_queue is not None
            and self.id_queue.empty()
        ):
            self.id_queue.put(None)  # resolver sentinel

    # ----------------------------------------------------------- pages
    def _pump_pages(self) -> None:
        """Start collecting the next page link (one in-app window at a time).

        Called whenever a link is added or a page finishes; page links added
        mid-run simply wait here behind the one currently being collected.
        """
        if not self._session_open:
            return
        if self.fetch_stop.is_set():
            # Stop pressed - drop everything still queued and end the session
            self._page_queue.clear()
            self._session_sources = 0
            self._maybe_finish_session()
            return
        if self._page_dialog is not None or self._edge_page_running:
            return
        if not self._page_queue:
            return
        url = self._page_queue.pop(0)
        kind = xsearch.page_kind(url)

        if xsearch.is_chat_url(url) and not xweb.webengine_available():
            self.ctl.fetchError.emit(
                f"{url}: DM chats need the built-in browser, which is not "
                "available in this build."
            )
            self._source_closed()
            QTimer.singleShot(0, self._pump_pages)
            return

        if xweb.webengine_available():
            self.set_status(f"Opening the {kind} page inside the app...", MUTED)
            try:
                self._page_dialog = xweb.run_page_dialog_async(
                    self,
                    url,
                    progress=lambda msg: self.set_status(msg, MUTED),
                    stop_event=self.fetch_stop,
                    on_new_ids=self._queue_new_ids,
                    on_done=self._on_page_done,
                )
            except Exception as exc:  # defensive - never kill the app
                self._page_dialog = None
                self.ctl.fetchError.emit(
                    f"The in-app browser window failed: {exc}"
                )
                self._source_closed()
                QTimer.singleShot(0, self._pump_pages)
        else:
            self._edge_page_running = True
            self.set_status(
                "Built-in browser unavailable - using Edge instead...", AMBER
            )
            threading.Thread(
                target=self._edge_page_worker, args=(url,), daemon=True
            ).start()

    def _edge_page_worker(self, url: str) -> None:
        """Fallback page collector (Edge) for when QtWebEngine is absent."""
        result: dict = {
            "found": {}, "stopped": False, "error": None,
            "dm_urls": [], "dm_posters": [], "kind": xsearch.page_kind(url),
        }
        try:
            collector = xsearch.SearchCollector(
                url,
                progress=lambda msg: self.ctl.fetchStarted.emit(msg),
                on_items=lambda chunk: self.ctl.metaReady.emit(chunk),
                stop_event=self.fetch_stop,
            )
            collector.collect()
        except core.XGrabError as exc:
            result["error"] = str(exc)
        except Exception as exc:  # defensive
            result["error"] = f"Unexpected error: {exc}"
        self.ctl.pageDone.emit(result)

    def _on_page_done(self, result: dict) -> None:
        self._page_dialog = None
        self._edge_page_running = False
        self._handle_page_result(result)
        self._source_closed()
        QTimer.singleShot(0, self._pump_pages)

    def _handle_page_result(self, result: dict) -> None:
        err = result.get("error")
        if err:
            self.ctl.fetchError.emit(err)
        dm_urls = result.get("dm_urls") or []
        if dm_urls:
            items = xweb.build_dm_items(dm_urls, result.get("dm_posters"))
            if items:
                self.ctl.metaReady.emit(items)
            elif not err:
                self.ctl.fetchError.emit(
                    "The chat was walked but no downloadable video stream "
                    "was captured."
                )
            return
        found = result.get("found") or {}
        # on_new_ids already queued everything found during scrolling; push
        # the full set again (a no-op for resolved ones) as a safety net.
        for tid in found:
            self._queue_new_ids([tid])
        if not found and not err and not result.get("stopped"):
            self.ctl.fetchError.emit(
                "Scrolled the whole page but found no video posts - the "
                "page may contain no videos."
            )

    def _queue_new_ids(self, ids: list) -> None:
        if self.id_queue is not None:
            for tid in ids:
                self.id_queue.put(tid)

    def _source_closed(self) -> None:
        self._session_sources = max(0, self._session_sources - 1)
        self._maybe_finish_session()

    def _on_fetch_started(self, text: str) -> None:
        self.set_status(text, MUTED)

    def _on_meta(self, items: list) -> None:
        if not items:
            return
        for it in items:
            self.items.append(it)
            self.item_map[it.key] = it
        # Show results as each link resolves instead of waiting for the
        # whole batch (which can be minutes for a large paste) - the user
        # sees videos land live rather than a long silent wait.
        self._refresh_results_ui()
        self.set_status(
            f"{len(self.items)} video{'s' if len(self.items) != 1 else ''} "
            f"found so far - still fetching...",
            MUTED,
        )

    def _on_fetch_error(self, err: str) -> None:
        self.set_status(err, RED)
        # A modal warning only when a fetch is NOT running at all and
        # nothing was found - per-page failures during a multi-link run
        # stay in the status line so a bad link never interrupts the rest.
        if not self.items and not self._session_open:
            QMessageBox.warning(self, core.APP_NAME, err)

    def _refresh_results_ui(self) -> None:
        self._clear_results()
        self.render_cards()
        self._start_thumbs()
        opts = ["Best available"] + core.quality_options(self.items)
        prev = self.quality_box.currentText()
        self.quality_box.clear()
        self.quality_box.addItems(opts)
        if prev in opts:
            self.quality_box.setCurrentText(prev)
        self._update_counter()

    def _on_fetch_done(self) -> None:
        self.fetching = False
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch videos")
        if self.items:
            self._refresh_results_ui()
            fail_links = self._ids_fail
            summary = (
                f"{len(self.items)} video{'s' if len(self.items) != 1 else ''} found"
            )
            if self._session_total:
                summary += (
                    f" from {self._session_total} "
                    f"link{'s' if self._session_total != 1 else ''}"
                )
            if fail_links:
                summary += (
                    f" ({fail_links} link{'s' if fail_links != 1 else ''} "
                    "failed - see above)"
                )
            summary += " - pick and press Download selected"
            self.set_status(summary, GREEN if not fail_links else AMBER)
        elif not self._session_open:
            self.set_status("No videos found in the fetched links.", AMBER)

    # ------------------------------------------------------------ results
    def _clear_results(self):
        while self._outer_layout.count():
            item = self._outer_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.cards = {}
        self._outer_layout.addStretch(1)

    def set_view(self, view: str) -> None:
        self.view = view
        active = "QPushButton#active"
        self.grid_btn.setStyleSheet(
            f"background: {ACCENT}; color: {BG};" if view == "grid" else ""
        )
        self.list_btn.setStyleSheet(
            f"background: {ACCENT}; color: {BG};" if view == "list" else ""
        )
        if self.items:
            self.render_cards()
        self._save_cfg()

    def render_cards(self) -> None:
        self._clear_results()
        if self.view == "grid":
            self._render_grid()
        else:
            self._render_list()

    def _render_grid(self) -> None:
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        width = max(self.scroll.viewport().width(), 500)
        cols = max(1, (width - 12) // (GRID_CARD_W + 12))
        cols = min(cols, max(1, len(self.items)))  # no empty trailing columns
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        for i, item in enumerate(self.items):
            r, c = divmod(i, cols)
            grid.addWidget(self._grid_card(item), r, c)
        self._outer_layout.insertWidget(0, holder)
        self._outer_layout.setStretch(0, 0)
        holder.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)

    def _grid_card(self, item: core.VideoItem) -> QWidget:
        key = item.key
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(GRID_CARD_W)
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 8, 8, 6)
        v.setSpacing(4)

        thumb = QLabel()
        pm = self.pixmaps.get(key) or _placeholder_pixmap(GRID_CARD_W, GRID_THUMB_H)
        thumb.setPixmap(pm.scaled(GRID_CARD_W, GRID_THUMB_H, Qt.KeepAspectRatioByExpanding,
                                  Qt.SmoothTransformation))
        thumb.setFixedSize(GRID_CARD_W, GRID_THUMB_H)
        thumb.setAlignment(Qt.AlignCenter)
        overlay = QHBoxLayout(thumb)
        cb = QCheckBox()
        overlay.addWidget(cb, 0, Qt.AlignTop | Qt.AlignLeft)
        overlay.addStretch(1)
        badge = QLabel(f"up to {item.quality_label}"
                       + (f"  ·  {item.duration_label}" if item.duration_label else ""))
        badge.setStyleSheet(
            "background: rgba(0,0,0,190); color: #FFFFFF; font-size: 8pt;"
            "font-weight: bold; padding: 2px 6px; border-radius: 4px;"
        )
        overlay.addWidget(badge, 0, Qt.AlignBottom | Qt.AlignRight)
        v.addWidget(thumb)

        title = QLabel(item.title)
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 9pt;")
        title.setMaximumHeight(40)
        v.addWidget(title)

        meta = QLabel(f"@{item.uploader}")
        meta.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        v.addWidget(meta)

        status = QLabel("")
        status.setWordWrap(True)
        status.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        status.setMinimumHeight(18)
        v.addWidget(status)

        st = self.status_state.get(key)
        if st:
            status.setText(st[0])
            status.setStyleSheet(f"color: {st[1]}; font-size: 8pt;")
        self.cards[key] = {"card": card, "thumb": thumb, "check": cb, "status": status,
                           "size": (GRID_CARD_W, GRID_THUMB_H)}
        cb.setChecked(True)
        cb.toggled.connect(lambda _on, k=key: self._update_counter())
        return card

    def _render_list(self) -> None:
        for item in self.items:
            self._outer_layout.insertWidget(
                self._outer_layout.count() - 1, self._list_row(item)
            )

    def _list_row(self, item: core.VideoItem) -> QWidget:
        key = item.key
        row = QFrame()
        row.setObjectName("card")
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 8, 12, 8)
        h.setSpacing(10)

        thumb = QLabel()
        pm = self.pixmaps.get(key) or _placeholder_pixmap(LIST_THUMB_W, LIST_THUMB_H)
        thumb.setPixmap(pm.scaled(LIST_THUMB_W, LIST_THUMB_H,
                                  Qt.KeepAspectRatioByExpanding,
                                  Qt.SmoothTransformation))
        thumb.setFixedSize(LIST_THUMB_W, LIST_THUMB_H)
        thumb.setAlignment(Qt.AlignCenter)
        h.addWidget(thumb)

        mid = QVBoxLayout()
        mid.setSpacing(2)
        title = QLabel(item.title)
        title.setWordWrap(True)
        mid.addWidget(title)
        meta = QLabel(
            f"@{item.uploader}  ·  up to {item.quality_label}"
            + (f"  ·  {item.duration_label}" if item.duration_label else "")
        )
        meta.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        mid.addWidget(meta)
        status = QLabel("")
        status.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        status.setMinimumHeight(18)
        mid.addWidget(status)
        st = self.status_state.get(key)
        if st:
            status.setText(st[0])
            status.setStyleSheet(f"color: {st[1]}; font-size: 8pt;")
        h.addLayout(mid, 1)

        cb = QCheckBox()
        cb.setChecked(True)
        cb.toggled.connect(lambda _on, k=key: self._update_counter())
        h.addWidget(cb, 0, Qt.AlignTop | Qt.AlignRight)

        self.cards[key] = {"card": row, "thumb": thumb, "check": cb, "status": status,
                           "size": (LIST_THUMB_W, LIST_THUMB_H)}
        return row

    def _start_thumbs(self) -> None:
        # Called once per link now (progressive results), not just once for
        # the whole batch - only submit items that haven't been requested
        # yet, or a 256-link batch would resubmit the same early items'
        # thumbnail jobs hundreds of times.
        def job(item: core.VideoItem):
            data = None
            try:
                data = core.fetch_thumbnail_bytes(item, "small")
            except Exception:
                data = None
            if not data and item.thumbnail_url:
                # Direct fetch failed (some VPNs, proxies and ISP-level
                # blocks stop plain urllib reaching pbs.twimg.com while the
                # in-app browser still works). Route the image through the
                # embedded browser session - capped so a fully offline
                # machine does not trigger hundreds of page loads.
                if self._thumb_browser_failures < 6:
                    data = self.ctl.fetch_thumb_via_browser_sync(
                        item.thumbnail_url
                    )
                    if not data:
                        self._thumb_browser_failures += 1
                else:
                    return  # placeholder stays
            if data:
                self.ctl.thumbReady.emit(item.key, data)

        pending = [
            it for it in self.items
            if it.key not in self._thumb_requested and it.thumbnail_url
        ]
        if not pending:
            return
        pool = ThreadPoolExecutor(max_workers=THUMB_POOL)
        for it in pending:
            self._thumb_requested.add(it.key)
            pool.submit(job, it)
        pool.shutdown(wait=False)

    def _on_thumb(self, key: tuple, data: bytes) -> None:
        try:
            pm = QPixmap()
            pm.loadFromData(data)
            if not pm.isNull():
                self.pixmaps[key] = pm
                refs = self.cards.get(key)
                if refs:
                    w, h = refs["size"]
                    refs["thumb"].setPixmap(
                        pm.scaled(w, h, Qt.KeepAspectRatioByExpanding,
                                  Qt.SmoothTransformation)
                    )
        except Exception:
            pass

    # ---------------------------------------------------------- selection
    def select_all(self) -> None:
        for refs in self.cards.values():
            refs["check"].setChecked(True)
        self._update_counter()

    def select_none(self) -> None:
        for refs in self.cards.values():
            refs["check"].setChecked(False)
        self._update_counter()

    def _selected_keys(self) -> list[tuple]:
        return [k for k, refs in self.cards.items() if refs["check"].isChecked()]

    def _update_counter(self) -> None:
        n = len(self._selected_keys())
        self.counter_lbl.setText(f"{n} of {len(self.cards)} selected")

    # ---------------------------------------------------------- downloads
    def choose_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.output_dir or os.path.expanduser("~")
        )
        if d:
            self.output_dir = d
            self.folder_lbl.setText(self._short_dir())
            self._save_cfg()

    def open_multi_dialog(self) -> None:
        """Open (or raise) the 'Multiple pages' paste window."""
        if self._multi_dlg is None:
            self._multi_dlg = MultiPagesDialog(self)
        self._multi_dlg.show()
        self._multi_dlg.raise_()
        self._multi_dlg.activateWindow()

    def quality_cap(self):
        val = self.quality_box.currentText()
        if val.endswith("p") and val[:-1].isdigit():
            return int(val[:-1])
        return None

    def start_download(self) -> None:
        if self.downloading or self.fetching:
            return
        if not self.items:
            self.set_status("Fetch a post link first.", AMBER)
            return
        selected = self._selected_keys()
        if not selected:
            self.set_status("Nothing selected - tick at least one video.", AMBER)
            return
        folder = self.output_dir
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                folder = QFileDialog.getExistingDirectory(self, "Choose download folder")
                if not folder:
                    return
                self.output_dir = folder
                self.folder_lbl.setText(self._short_dir())
        self._save_cfg()

        self.downloading = True
        self.cancel_event.clear()
        self._dl_done = self._dl_ok = self._dl_fail = 0
        self._dl_total = len(selected)
        self.progress.setRange(0, self._dl_total)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        for k in selected:
            self.set_item_status(k, "Queued", MUTED)
        for w in (self.download_btn, self.fetch_btn):
            w.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.set_status(
            f"Downloading {self._dl_total} video"
            f"{'s' if self._dl_total > 1 else ''} to {folder}...", ACCENT
        )

        cap = self.quality_cap()

        def worker(key: tuple):
            item = self.item_map[key]
            try:
                fmt = core.pick_format(item, cap)
                name = core.build_filename(item, fmt)
                dest = core.unique_path(os.path.join(folder, name))

                def cb(done, total):
                    self.ctl.itemProgress.emit(key, done, total)

                core.download_file(fmt.url, dest, progress_cb=cb,
                                   cancel_event=self.cancel_event)
                self.ctl.itemDone.emit(key, dest)
            except core.XGrabError as exc:
                self.ctl.itemFail.emit(key, str(exc))
            except Exception as exc:  # defensive
                self.ctl.itemFail.emit(key, f"Unexpected error: {exc}")

        def runner():
            pool = ThreadPoolExecutor(max_workers=DL_POOL)
            futures = [pool.submit(worker, k) for k in selected]
            pool.shutdown(wait=True)
            self.ctl.allDone.emit()

        threading.Thread(target=runner, daemon=True).start()

    def cancel_download(self) -> None:
        if self.downloading:
            self.cancel_event.set()
            self.set_status("Cancelling...", AMBER)

    def _on_progress(self, key: tuple, done: int, total) -> None:
        pct = f" · {int(done * 100 / total)}%" if total else ""
        size = core.human_size(total) if total else core.human_size(done)
        self.set_item_status(
            key, f"Downloading{pct} · {core.human_size(done)} / {size}", ACCENT
        )

    def _on_item_done(self, key: tuple, path: str) -> None:
        self._dl_done += 1
        self._dl_ok += 1
        self.progress.setValue(self._dl_done)
        self.set_item_status(key, f"Saved - {self._short_name(os.path.basename(path))}", GREEN)

    def _on_item_fail(self, key: tuple, err: str) -> None:
        self._dl_done += 1
        self._dl_fail += 1
        self.progress.setValue(self._dl_done)
        self.set_item_status(key, f"Failed - {err}", RED)

    def _on_all_done(self) -> None:
        self.downloading = False
        self.cancel_btn.setEnabled(False)
        for w in (self.download_btn, self.fetch_btn):
            w.setEnabled(True)
        self.open_btn.setEnabled(True)
        if self.cancel_event.is_set():
            self.set_status(
                f"Cancelled - {self._dl_ok} saved, {self._dl_fail} failed", AMBER
            )
        else:
            self.set_status(
                f"Done - {self._dl_ok} saved"
                + (f", {self._dl_fail} failed" if self._dl_fail else "")
                + f"  ({self.output_dir})",
                GREEN if not self._dl_fail else AMBER,
            )

    def open_folder(self) -> None:
        path = self.output_dir
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            QMessageBox.information(self, core.APP_NAME, path)

    # ------------------------------------------------------------ plumbing
    def set_status(self, text: str, color: str) -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(f"color: {color};")

    def set_item_status(self, key: tuple, text: str, color: str) -> None:
        self.status_state[key] = (text, color)
        refs = self.cards.get(key)
        if refs:
            refs["status"].setText(text)
            refs["status"].setStyleSheet(f"color: {color}; font-size: 8pt;")

    @staticmethod
    def _short_name(name: str, limit: int = 34) -> str:
        if len(name) <= limit:
            return name
        return name[:16] + "..." + name[-(limit - 19):]

    def _short_dir(self) -> str:
        d = self.output_dir
        return ("..." + d[-43:]) if len(d) > 46 else d

    def _save_cfg(self) -> None:
        core.save_config({"output_dir": self.output_dir, "view": self.view})

    def _reflow_if_needed(self) -> None:
        if self.view == "grid" and self.items:
            self.render_cards()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.view == "grid" and self.items:
            width = self.scroll.viewport().width()
            cols = min(
                max(1, (width - 12) // (GRID_CARD_W + 12)), max(1, len(self.items))
            )
            grid = self._find_grid()
            if grid is not None and grid.columnCount() != cols:
                self.render_cards()

    def _find_grid(self):
        if self._outer_layout.count():
            item = self._outer_layout.itemAt(0)
            w = item.widget() if item else None
            if w is not None and w.layout() is not None:
                return w.layout()
        return None

    def closeEvent(self, event) -> None:
        self.cancel_event.set()
        self.fetch_stop.set()
        if self._page_dialog is not None:
            try:
                self._page_dialog.close()
            except Exception:
                pass
        if self._multi_dlg is not None:
            try:
                self._multi_dlg.close()
            except Exception:
                pass
        try:
            self._save_cfg()
        except Exception:
            pass
        super().closeEvent(event)
        # Hard guarantee the app actually exits here, rather than relying
        # on Qt's implicit "quit when the last window closes" tracking -
        # any other top-level widget still counted as open (even one the
        # user never sees) silently defeats that and leaves the process
        # running with no visible window, which is exactly what was
        # reported. Closing the main window should always mean the app is
        # gone, full stop.
        QApplication.instance().quit()


def main() -> None:
    # QtWebEngine (the in-app browser used for search pages) requires this
    # attribute to be set BEFORE the QApplication is constructed.
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    # QtWebEngine backs every page with its own child process; one left
    # behind by Qt/Python's ordinary object teardown at process exit is
    # exactly what keeps the app running in the background after the main
    # window closes. Tear it down explicitly, every time, before the event
    # loop actually stops.
    app.aboutToQuit.connect(xweb.shutdown_resolve_view)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
