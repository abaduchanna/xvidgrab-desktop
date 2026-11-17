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
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
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
        self.search_running = False
        self.fetching = False
        self.downloading = False
        self._dl_done = self._dl_ok = self._dl_fail = self._dl_total = 0

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
        sub = QLabel("Paste an X post link - or a search link (x.com/search?q=...) - search pages open inside the app, browser-free")
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
            "https://x.com/user/status/123... or https://x.com/search?q=..."
            "   (several post links in one paste are fine)"
        )
        self.url_edit.returnPressed.connect(self.start_fetch)
        urlrow.addWidget(self.url_edit, 1)
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
            "Paste an X post link above and press Fetch videos\n\n"
            "Every video on that page shows up here with a thumbnail\n\n"
            "Multiple links in one paste are fine too"
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

    # -------------------------------------------------------------- fetch
    def start_fetch(self) -> None:
        # clicking while a search collection runs stops it (keeps results)
        if self.search_running:
            self.fetch_stop.set()
            self.set_status("Stopping collection - please wait...", AMBER)
            return
        if self.fetching or self.downloading:
            return
        text = self.url_edit.text().strip()

        search_url = xsearch.is_search_url(text)
        if not search_url and core._TCO_RE.search(text):
            try:
                search_url = xsearch.is_search_url(core.expand_tco_links(text))
            except Exception:
                search_url = None

        def do_fetch():
            urls = core.parse_status_urls(text)
            if not urls and core._TCO_RE.search(text):
                try:
                    urls = core.parse_status_urls(core.expand_tco_links(text))
                except Exception:
                    urls = []
            if not urls:
                self.ctl.fetchError.emit(
                    "No X post link or search link found in the input.\n"
                    "Paste a link like x.com/user/status/123...\n"
                    "or a search page like x.com/search?q=videos"
                )
                return
            self.ctl.fetchStarted.emit(
                f"Fetching {len(urls)} link{'s' if len(urls) > 1 else ''}..."
            )
            ok_count = 0
            fail_count = 0
            for i, u in enumerate(urls):
                if i > 0:
                    # A large batch hammering X's syndication endpoint with
                    # no gap trips its rate limit after a handful of
                    # requests, silently failing the rest - space them out.
                    time.sleep(core.BATCH_FETCH_DELAY)
                try:
                    items = core.fetch_videos(u)
                    self.ctl.metaReady.emit(items)
                    ok_count += 1
                except core.XGrabError as exc:
                    self.ctl.fetchError.emit(str(exc))
                    fail_count += 1
                except Exception as exc:  # defensive
                    self.ctl.fetchError.emit(f"Unexpected error: {exc}")
                    fail_count += 1
            self._last_fetch_ok = ok_count
            self._last_fetch_fail = fail_count
            self._last_fetch_total = len(urls)
            self.ctl.fetchDone.emit()

        self.fetching = True
        # reset previous results
        self.items = []
        self.item_map = {}
        self.cards = {}
        self.pixmaps = {}
        self._thumb_requested = set()
        self.status_state = {}
        self._last_fetch_ok = 0
        self._last_fetch_fail = 0
        self._last_fetch_total = 0
        self._clear_results()
        self._update_counter()
        if search_url:
            self._begin_search(search_url)
            return
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching...")
        self.set_status("Fetching...", MUTED)
        threading.Thread(target=do_fetch, daemon=True).start()

    # ------------------------------------------------------ search (in-app)
    def _begin_search(self, search_url: str) -> None:
        """Search links: collect inside the in-app browser window.

        The embedded window (QtWebEngine) loads the page, hosts the X login
        when needed and auto-scrolls to the end - nothing ever opens outside
        the app.  Only when the embedded browser is unavailable does the
        legacy Edge automation run as a fallback.
        """
        self.search_running = True
        self.fetch_stop.clear()
        # keep the button alive as a Stop control during collection
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Stop")
        if not xweb.webengine_available():
            self.set_status(
                "Built-in browser unavailable - using Edge instead...", AMBER
            )
            threading.Thread(
                target=lambda: self._do_search_fetch(search_url), daemon=True
            ).start()
            return
        self.set_status("Opening the search page inside the app...", MUTED)
        try:
            result = xweb.run_search_dialog(
                self,
                search_url,
                progress=lambda msg: self.set_status(msg, MUTED),
                stop_event=self.fetch_stop,
            )
        except Exception as exc:  # defensive - the dialog must never kill the app
            self.ctl.fetchError.emit(f"The in-app search window failed: {exc}")
            self.ctl.fetchDone.emit()
            return
        self._after_search_dialog(result)

    def _after_search_dialog(self, result: dict) -> None:
        """Dialog closed: resolve whatever was collected in a worker thread."""
        found = result.get("found") or {}
        err = result.get("error")
        if err:
            self.ctl.fetchError.emit(err)
            self.ctl.fetchDone.emit()
            return
        if not found:
            if result.get("stopped"):
                self.set_status("Collection stopped.", AMBER)
            else:
                self.ctl.fetchError.emit(
                    "Scrolled the whole page but found no video posts.\n"
                    "The search results may contain no videos."
                )
            self.ctl.fetchDone.emit()
            return

        def worker():
            try:
                xsearch.resolve_ids(
                    list(found),
                    progress=lambda m: self.ctl.fetchStarted.emit(m),
                    on_items=lambda chunk: self.ctl.metaReady.emit(chunk),
                    stop_event=self.fetch_stop,
                )
            except Exception as exc:  # defensive
                self.ctl.fetchError.emit(f"Unexpected error: {exc}")
            finally:
                self.search_running = False
                self.ctl.fetchDone.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _do_search_fetch(self, search_url: str) -> None:
        """Worker (fallback only): scroll the page in Edge and resolve."""
        try:
            collector = xsearch.SearchCollector(
                search_url,
                progress=lambda msg: self.ctl.fetchStarted.emit(msg),
                on_items=lambda chunk: self.ctl.metaReady.emit(chunk),
                stop_event=self.fetch_stop,
            )
            collector.collect()
        except core.XGrabError as exc:
            self.ctl.fetchError.emit(str(exc))
        except Exception as exc:  # defensive
            self.ctl.fetchError.emit(f"Unexpected error: {exc}")
        finally:
            self.search_running = False
            self.ctl.fetchDone.emit()

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
        if not self.items:
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
        self.search_running = False
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch videos")
        if self.items:
            self._refresh_results_ui()
            total_links = getattr(self, "_last_fetch_total", 0)
            fail_links = getattr(self, "_last_fetch_fail", 0)
            ok_links = getattr(self, "_last_fetch_ok", 0)
            summary = (
                f"{len(self.items)} video{'s' if len(self.items) != 1 else ''} found"
            )
            if total_links:
                summary += (
                    f" from {ok_links}/{total_links} "
                    f"link{'s' if total_links != 1 else ''}"
                )
                if fail_links:
                    summary += f" ({fail_links} link{'s' if fail_links != 1 else ''} failed - see above)"
            summary += " - pick and press Download selected"
            self.set_status(summary, GREEN if not fail_links else AMBER)

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
            try:
                data = core.fetch_thumbnail_bytes(item, "small")
                self.ctl.thumbReady.emit(item.key, data)
            except Exception:
                pass  # placeholder stays

        pending = [it for it in self.items if it.key not in self._thumb_requested]
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
        try:
            self._save_cfg()
        except Exception:
            pass
        super().closeEvent(event)


def main() -> None:
    # QtWebEngine (the in-app browser used for search pages) requires this
    # attribute to be set BEFORE the QApplication is constructed.
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
