#!/usr/bin/env python3
"""XVidGrab Desktop - Tkinter GUI.

Paste an X (Twitter) post link, see every video on the page with
thumbnails, pick the ones you want (or Select All) and download them at
the highest available resolution. List view and grid view included.

Run:  python xvidgrab_desktop.py

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
from io import BytesIO
from tkinter import BOTH, BOTTOM, END, LEFT, RIGHT, TOP, W, X, Y, filedialog, messagebox, ttk
import tkinter as tk

from PIL import Image, ImageTk

import xgrab_core as core
import xgrab_search as xsearch

# 3SVerse brand palette (same tokens as License Studio / WiFi Transfer)
BG = "#07060b"
SURFACE = "#0d0c14"
CARD = "#0d0c14"
CARD_SEL = "#10242a"
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
GRID_THUMB_H = 112  # 16:9
LIST_THUMB_W = 160
LIST_THUMB_H = 90
THUMB_POOL = 6
DL_POOL = 3


def make_placeholder(w: int, h: int) -> Image.Image:
    """Dark thumbnail placeholder with a play glyph."""
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
    return img.resize((w, h), Image.LANCZOS)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{core.APP_NAME}")
        self.minsize(960, 620)
        self.configure(bg=BG)
        try:
            self.iconphoto(
                True,
                ImageTk.PhotoImage(
                    Image.open(core.resource_path("icons/icon-128.png")).resize((64, 64))
                ),
            )
        except Exception:
            pass

        cfg = core.load_config()
        self.output_dir: str = cfg.get("output_dir") or core.default_download_dir()
        self.view: str = cfg.get("view") if cfg.get("view") in ("grid", "list") else "grid"

        self.items: list[core.VideoItem] = []
        self.item_map: dict[tuple, core.VideoItem] = {}
        self.sel_vars: dict[tuple, tk.IntVar] = {}
        self.photos: dict[tuple, ImageTk.PhotoImage] = {}
        self.status_state: dict[tuple, tuple[str, str]] = {}  # key -> (text, color)
        self.status_labels: dict[tuple, tk.Label] = {}
        self.thumb_label: dict[tuple, tk.Label] = {}

        self._last_grid_cols = 0
        self.msg_q: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.fetch_stop = threading.Event()
        self.search_running = False
        self.fetching = False
        self.downloading = False
        self._dl_done = 0
        self._dl_total = 0
        self._dl_ok = 0
        self._dl_fail = 0
        self._last_urls: list[str] = []

        self._build()
        self.after(80, self._poll_queue)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        # Header --------------------------------------------------------
        head = tk.Frame(self, bg=BG)
        head.pack(fill=X, padx=18, pady=(14, 6))
        try:
            logo = Image.open(core.resource_path("icons/icon-128.png")).resize((34, 34))
            self._logo_img = ImageTk.PhotoImage(logo)
            tk.Label(head, image=self._logo_img, bg=BG).pack(side=LEFT, padx=(0, 10))
        except Exception:
            pass
        hleft = tk.Frame(head, bg=BG)
        hleft.pack(side=LEFT)
        tk.Label(
            hleft, text=core.APP_NAME, bg=BG, fg=TEXT,
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor=W)
        tk.Label(
            hleft,
            text="Paste an X post link - or a search link (x.com/search?q=...) to grab every video on the page",
            bg=BG, fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor=W)
        tk.Label(
            head, text=f"v{core.__version__}", bg=BG, fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side=RIGHT, pady=(6, 0))

        self._build_footer()

        # URL row --------------------------------------------------------
        urlrow = tk.Frame(self, bg=BG)
        urlrow.pack(fill=X, padx=18, pady=(6, 4))
        self.url_entry = tk.Entry(
            urlrow, bg=SURFACE, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            font=("Segoe UI", 11),
        )
        self.url_entry.pack(side=LEFT, fill=X, expand=True, ipady=7, padx=(0, 8))
        self.url_entry.insert(
            0, "https://x.com/..."
        )
        self.url_entry.bind("<FocusIn>", self._url_focus_in)
        self.url_entry.bind("<Return>", lambda e: self.start_fetch())
        self.fetch_btn = self._btn(urlrow, "Fetch videos", self.start_fetch, primary=True)
        self.fetch_btn.pack(side=LEFT)

        # Toolbar --------------------------------------------------------
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill=X, padx=18, pady=(2, 6))
        self.view_grid_btn = self._btn(bar, "  Grid  ", lambda: self.set_view("grid"))
        self.view_list_btn = self._btn(bar, "  List  ", lambda: self.set_view("list"))
        self.view_grid_btn.pack(side=LEFT)
        self.view_list_btn.pack(side=LEFT, padx=(0, 12))
        self._btn(bar, "Select all", self.select_all).pack(side=LEFT)
        self._btn(bar, "Select none", self.select_none).pack(side=LEFT, padx=(4, 12))
        self.counter_lbl = tk.Label(
            bar, text="0 of 0 selected", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")
        )
        self.counter_lbl.pack(side=LEFT)
        # right side
        self.folder_lbl = tk.Label(
            bar, text=self._short_dir(), bg=BG, fg=MUTED,
            font=("Segoe UI", 8), cursor="hand2",
        )
        self.folder_lbl.pack(side=RIGHT, padx=(8, 0))
        self.folder_lbl.bind("<Button-1>", lambda e: self.choose_folder())
        self._btn(bar, "Folder...", self.choose_folder).pack(side=RIGHT)
        self.quality_box = ttk.Combobox(
            bar, values=["Best available"], state="readonly", width=14,
            font=("Segoe UI", 9), style="XGrab.TCombobox",
        )
        self.quality_box.set("Best available")
        self.quality_box.pack(side=RIGHT, padx=(8, 4))
        tk.Label(bar, text="Quality:", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(
            side=RIGHT
        )

        # Results scroll area ---------------------------------------------
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill=BOTH, expand=True, padx=18)
        self.canvas = tk.Canvas(
            wrap, bg=BG, highlightthickness=0, bd=0, selectbackground=BG
        )
        self.scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side=RIGHT, fill=Y)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._inner_win = self.canvas.create_window((0, 0), window=self.inner,
                                                    anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>", self._on_canvas_configure
        )
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

        self.empty_lbl = tk.Label(
            self.inner,
            text="\n\nPaste an X post link above and press Fetch videos\n\n"
                 "Every video on that page shows up here with a thumbnail\n\n"
                 "Multiple links in one paste are fine too",
            bg=BG, fg=MUTED, font=("Segoe UI", 11),
            justify="center",
        )
        self.empty_lbl.grid(row=0, column=0, pady=60)

        # Bottom bar -------------------------------------------------------
        bottom = tk.Frame(self, bg=SURFACE)
        bottom.pack(fill=X, side=BOTTOM, padx=18, pady=(6, 10))
        self.progress = ttk.Progressbar(
            bottom, mode="determinate", style="XGrab.Horizontal.TProgressbar"
        )
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=(0, 10), pady=8)
        self.status_lbl = tk.Label(
            bottom, text="Ready.", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)
        )
        self.status_lbl.pack(side=LEFT, padx=(0, 8), pady=6)
        self.open_btn = self._btn(bottom, "Open folder", self.open_folder)
        self.open_btn.pack(side=RIGHT, padx=(8, 0))
        self.open_btn.configure(state="disabled")
        self.cancel_btn = self._btn(bottom, "Cancel", self.cancel_download)
        self.cancel_btn.configure(state="disabled")
        self.cancel_btn.pack(side=RIGHT, padx=(8, 0))
        self.download_btn = self._btn(bottom, "Download selected", self.start_download,
                                      primary=True)
        self.download_btn.pack(side=RIGHT, padx=(8, 0))

        self.progress.pack_forget()  # only visible while downloading

        self.set_view(self.view)

    def _build_footer(self) -> None:
        """3SVerse brand footer (same standard as License Studio / WiFi
        Transfer): developed-by centered, independent of the version label."""
        bar = tk.Frame(self, bg=SURFACE, highlightbackground=BORDER,
                        highlightthickness=1)
        bar.pack(side=BOTTOM, fill=X)
        tk.Frame(bar, bg=SURFACE, height=28).pack(fill=X)
        tk.Label(bar, text="DEVELOPED BY WWW.3SVERSE.COM", bg=SURFACE,
                 fg=MUTED, font=("Segoe UI", 8, "bold")
                 ).place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(bar, text=f"v{core.__version__}", bg=SURFACE, fg=MUTED,
                 font=("Consolas", 8)).place(relx=1.0, rely=0.5, anchor="e", x=-14)

    def _grid_cols(self, width: int) -> int:
        cols = max(1, (width - 12) // (GRID_CARD_W + 12))
        return min(cols, max(1, len(self.items)))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._inner_win, width=event.width)
        # re-flow the grid when the column count changes
        if self.view == "grid" and self.items and not self.fetching:
            cols = self._grid_cols(event.width)
            if cols != self._last_grid_cols:
                self._last_grid_cols = cols
                self.render_cards()

    def _btn(self, parent, text, cmd, primary=False):
        b = tk.Button(
            parent, text=text, command=cmd,
            bg=ACCENT if primary else SURFACE,
            fg=BG if primary else TEXT,
            activebackground=ACCENT_HOVER if primary else CARD,
            activeforeground=BG if primary else TEXT,
            disabledforeground=MUTED,
            relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            padx=14, pady=6,
        )
        return b

    def _short_dir(self) -> str:
        d = self.output_dir
        if len(d) > 46:
            d = "..." + d[-43:]
        return d

    def _url_focus_in(self, _e):
        if self.url_entry.get().strip() in ("https://x.com/...",):
            self.url_entry.delete(0, END)

    def _on_wheel(self, e):
        delta = 0
        if getattr(e, "delta", None):
            delta = -1 * (e.delta // 120 if abs(e.delta) >= 120 else (1 if e.delta > 0 else -1))
        elif getattr(e, "num", None) == 4:
            delta = -1
        elif getattr(e, "num", None) == 5:
            delta = 1
        if delta:
            self.canvas.yview_scroll(delta, "units")

    # ------------------------------------------------------------- fetch
    def start_fetch(self) -> None:
        # clicking while a search collection runs stops it (keeps results)
        if self.search_running:
            self.fetch_stop.set()
            self.set_status("Stopping collection - please wait...", AMBER)
            return
        if self.fetching or self.downloading:
            return
        text = self.url_entry.get().strip()

        search_url = xsearch.is_search_url(text)
        if not search_url and core._TCO_RE.search(text):
            try:
                search_url = xsearch.is_search_url(core.expand_tco_links(text))
            except Exception:
                search_url = None

        def do_fetch():
            if search_url:
                self._do_search_fetch(search_url)
                return
            urls = core.parse_status_urls(text)
            if not urls and core._TCO_RE.search(text):
                try:
                    urls = core.parse_status_urls(core.expand_tco_links(text))
                except Exception:
                    urls = []
            if not urls:
                self.msg_q.put(("fetcherr", "", "No X post link or search link found in the input.\n"
                                "Paste a link like x.com/user/status/123...\n"
                                "or a search page like x.com/search?q=videos"))
                return
            self._last_urls = urls
            self.msg_q.put(("fetchstart", "", f"Fetching {len(urls)} link"
                             f"{'s' if len(urls) > 1 else ''}..."))
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
                    self.msg_q.put(("meta", u, items))
                    ok_count += 1
                except core.XGrabError as exc:
                    self.msg_q.put(("fetcherr", u, str(exc)))
                    fail_count += 1
                except Exception as exc:  # defensive
                    self.msg_q.put(("fetcherr", u, f"Unexpected error: {exc}"))
                    fail_count += 1
            self.msg_q.put(("fetchdone", "", (ok_count, fail_count, len(urls))))

        self.fetching = True
        # reset previous results
        self.items = []
        self.item_map = {}
        self.sel_vars = {}
        self.photos = {}
        self.status_state = {}
        self._last_grid_cols = 0
        self._clear_results()
        self._update_counter()
        self.progress.pack_forget()
        if search_url:
            self.search_running = True
            self.fetch_stop.clear()
            # keep the button alive as a Stop control during collection
            self.fetch_btn.configure(state="normal", text="Stop")
            self.set_status("Collecting every video from the search page...", MUTED)
        else:
            self.fetch_btn.configure(state="disabled", text="Fetching...")
            self.set_status("Fetching...", MUTED)
        threading.Thread(target=do_fetch, daemon=True).start()

    def _do_search_fetch(self, search_url: str) -> None:
        """Worker: scroll the search page in Edge and resolve every video."""
        try:
            collector = xsearch.SearchCollector(
                search_url,
                progress=lambda msg: self.msg_q.put(("fetchstart", "", msg)),
                on_items=lambda chunk: self.msg_q.put(("meta", search_url, chunk)),
                stop_event=self.fetch_stop,
            )
            collector.collect()
        except core.XGrabError as exc:
            self.msg_q.put(("fetcherr", search_url, str(exc)))
        except Exception as exc:  # defensive
            self.msg_q.put(("fetcherr", search_url, f"Unexpected error: {exc}"))
        finally:
            self.search_running = False
            self.msg_q.put(("fetchdone", "", ""))

    # ------------------------------------------------------------ results
    def _clear_results(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.status_labels.clear()
        self.thumb_label.clear()

    def set_view(self, view: str):
        self.view = view
        active, inactive = (ACCENT, SURFACE)
        self.view_grid_btn.configure(
            bg=active if view == "grid" else inactive,
            fg=BG if view == "grid" else TEXT)
        self.view_list_btn.configure(
            bg=active if view == "list" else inactive,
            fg=BG if view == "list" else TEXT)
        self.render_cards()
        self._save_cfg()

    def render_cards(self):
        if not self.items:
            return
        self._clear_results()
        if self.view == "grid":
            self._render_grid()
        else:
            self._render_list()

    def _render_grid(self):
        width = max(self.canvas.winfo_width(), 500) - 12
        cols = self._grid_cols(width)
        self._last_grid_cols = cols
        # reset stale column weights from an earlier layout, then spread evenly
        for c in range(self.inner.grid_size()[0]):
            self.inner.grid_columnconfigure(c, weight=0, uniform="")
        for c in range(cols):
            self.inner.grid_columnconfigure(c, weight=1, uniform="cards")
        for i, item in enumerate(self.items):
            r, c = divmod(i, cols)
            self._grid_card(item).grid(row=r, column=c, padx=6, pady=6, sticky="nw")

    def _grid_card(self, item: core.VideoItem) -> tk.Frame:
        key = item.key
        card = tk.Frame(self.inner, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER)
        # thumbnail
        thumb = tk.Label(card, bg=THUMB_BG, width=26, height=6)
        thumb.configure(anchor="center")
        self._apply_thumb(key, thumb, (GRID_CARD_W, GRID_THUMB_H))
        thumb.pack(fill=X, padx=2, pady=(2, 0))
        thumb.bind("<Button-1>", lambda e: self._toggle_item(key))
        # overlay: checkbox
        cbf = tk.Frame(thumb, bg="#000000")
        cb = tk.Checkbutton(
            cbf, variable=self.sel_vars[key], bg="#000000", fg=TEXT,
            activebackground="#000000", activeforeground=ACCENT,
            selectcolor="#FFFFFF", highlightthickness=0, bd=0, cursor="hand2",
        )
        cb.pack(padx=3, pady=2)
        cbf.place(x=4, y=4, anchor="nw")
        # overlay: quality badge (top-right of the thumbnail)
        qb = tk.Label(
            thumb, text=item.quality_label, bg="#000000", fg="#FFFFFF",
            font=("Segoe UI", 8, "bold"), padx=5,
        )
        qb.place(relx=1.0, y=4, x=-4, anchor="ne")
        if item.duration_label:
            db = tk.Label(
                thumb, text=item.duration_label, bg="#000000", fg="#FFFFFF",
                font=("Segoe UI", 8, "bold"), padx=4,
            )
            db.place(relx=1.0, rely=1.0, x=-4, y=-4, anchor="se")
        # body
        body = tk.Frame(card, bg=CARD)
        body.pack(fill=X, padx=8, pady=(6, 0))
        title = tk.Label(
            body, text=item.title, bg=CARD, fg=TEXT, wraplength=GRID_CARD_W - 22,
            justify=LEFT, font=("Segoe UI", 9), anchor="w",
        )
        title.pack(anchor=W)
        title.bind("<Button-1>", lambda e: self._toggle_item(key))
        meta = tk.Label(
            body, text=f"@{item.uploader}",
            bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w",
        )
        meta.pack(anchor=W)
        status = tk.Label(
            body, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w",
            wraplength=GRID_CARD_W - 22, height=2,
        )
        status.pack(anchor=W, pady=(2, 6))
        st = self.status_state.get(key)
        if st:
            status.configure(text=st[0], fg=st[1])
        self.status_labels[key] = status
        return card

    def _render_list(self):
        for item in self.items:
            self._list_row(item).pack(fill=X, padx=2, pady=3)

    def _list_row(self, item: core.VideoItem) -> tk.Frame:
        key = item.key
        row = tk.Frame(self.inner, bg=CARD, highlightthickness=1,
                       highlightbackground=BORDER)
        thumb = tk.Label(row, bg=THUMB_BG)
        self._apply_thumb(key, thumb, (LIST_THUMB_W, LIST_THUMB_H))
        thumb.pack(side=LEFT, padx=6, pady=6)
        mid = tk.Frame(row, bg=CARD)
        mid.pack(side=LEFT, fill=BOTH, expand=True, pady=6)
        title = tk.Label(
            mid, text=item.title, bg=CARD, fg=TEXT, wraplength=430,
            justify=LEFT, font=("Segoe UI", 10), anchor="w",
        )
        title.pack(anchor=W)
        title.bind("<Button-1>", lambda e: self._toggle_item(key))
        thumb.bind("<Button-1>", lambda e: self._toggle_item(key))
        meta = tk.Label(
            mid, text=f"@{item.uploader}  ·  up to {item.quality_label}"
            + (f"  ·  {item.duration_label}" if item.duration_label else ""),
            bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w",
        )
        meta.pack(anchor=W)
        status = tk.Label(
            mid, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w",
        )
        status.pack(anchor=W)
        st = self.status_state.get(key)
        if st:
            status.configure(text=st[0], fg=st[1])
        self.status_labels[key] = status
        cb = tk.Checkbutton(
            row, variable=self.sel_vars[key], bg=CARD, fg=TEXT,
            activebackground=CARD, activeforeground=ACCENT,
            selectcolor="#FFFFFF", highlightthickness=0, bd=0, cursor="hand2",
        )
        cb.pack(side=RIGHT, padx=12)
        return row

    def _apply_thumb(self, key, label: tk.Label, size):
        self.thumb_label[key] = label
        ph = self.photos.get(key)
        if ph is None:
            base = make_placeholder(*size)
            ph = ImageTk.PhotoImage(base)
        label.configure(image=ph, width=size[0], height=size[1])
        label.image = ph  # keep reference

    def _toggle_item(self, key):
        var = self.sel_vars.get(key)
        if var is not None:
            var.set(0 if var.get() else 1)
            self._update_counter()

    # ---------------------------------------------------------- selection
    def select_all(self):
        for v in self.sel_vars.values():
            v.set(1)
        self._update_counter()

    def select_none(self):
        for v in self.sel_vars.values():
            v.set(0)
        self._update_counter()

    def _update_counter(self):
        n = sum(1 for v in self.sel_vars.values() if v.get())
        self.counter_lbl.configure(text=f"{n} of {len(self.sel_vars)} selected")

    # ------------------------------------------------------- thumb worker
    def _start_thumbs(self):
        def job(item: core.VideoItem):
            try:
                data = core.fetch_thumbnail_bytes(item, "small")
                self.msg_q.put(("thumb", item.key, data))
            except Exception:
                pass  # placeholder stays

        pool = ThreadPoolExecutor(max_workers=THUMB_POOL)
        for it in self.items:
            pool.submit(job, it)
        pool.shutdown(wait=False)

    # ---------------------------------------------------------- downloads
    def choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.output_dir or os.path.expanduser("~"),
                                    title="Choose download folder")
        if d:
            self.output_dir = d
            self.folder_lbl.configure(text=self._short_dir())
            self._save_cfg()

    def quality_cap(self) -> int | None:
        val = self.quality_box.get()
        if val.endswith("p") and val[:-1].isdigit():
            return int(val[:-1])
        return None

    def start_download(self) -> None:
        if self.downloading or self.fetching:
            return
        if not self.items:
            self.set_status("Fetch a post link first.", AMBER)
            return
        selected = [k for k, v in self.sel_vars.items() if v.get()]
        if not selected:
            self.set_status("Nothing selected - tick at least one video.", AMBER)
            return
        folder = self.output_dir
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                folder = filedialog.askdirectory(title="Choose download folder")
                if not folder:
                    return
                self.output_dir = folder
                self.folder_lbl.configure(text=self._short_dir())
        self._save_cfg()

        self.downloading = True
        self.cancel_event.clear()
        self.cancel_btn.configure(state="normal")
        self._dl_done = self._dl_ok = self._dl_fail = 0
        self._dl_total = len(selected)
        self.progress.configure(maximum=self._dl_total, value=0)
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=(0, 10), pady=8,
                           before=self.status_lbl)
        for k in selected:
            self.set_item_status(k, "Queued", MUTED)
        for w in (self.download_btn, self.fetch_btn):
            w.configure(state="disabled")
        self.cancel_btn.pack(side=RIGHT, padx=(8, 0), before=self.open_btn)
        self.set_status(f"Downloading {self._dl_total} video"
                        f"{'s' if self._dl_total > 1 else ''} to {folder}...", ACCENT)

        cap = self.quality_cap()

        def worker(key):
            item = self.item_map[key]
            try:
                fmt = core.pick_format(item, cap)
                name = core.build_filename(item, fmt)
                dest = core.unique_path(os.path.join(folder, name))

                def cb(done, total):
                    self.msg_q.put(("progress", key, done, total))

                core.download_file(fmt.url, dest, progress_cb=cb,
                                   cancel_event=self.cancel_event)
                self.msg_q.put(("done", key, dest))
            except core.XGrabError as exc:
                self.msg_q.put(("fail", key, str(exc)))
            except Exception as exc:  # defensive
                self.msg_q.put(("fail", key, f"Unexpected error: {exc}"))

        def runner():
            pool = ThreadPoolExecutor(max_workers=DL_POOL)
            futures = [pool.submit(worker, k) for k in selected]
            pool.shutdown(wait=True)
            self.msg_q.put(("alldone", "", ""))

        threading.Thread(target=runner, daemon=True).start()

    def cancel_download(self):
        if self.downloading:
            self.cancel_event.set()
            self.set_status("Cancelling...", AMBER)

    def open_folder(self):
        path = self.output_dir
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            messagebox.showinfo(core.APP_NAME, path)

    # ------------------------------------------------------------ plumbing
    def set_status(self, text, color=MUTED):
        self.status_lbl.configure(text=text, fg=color)

    def set_item_status(self, key, text, color):
        self.status_state[key] = (text, color)
        lbl = self.status_labels.get(key)
        if lbl is not None:
            lbl.configure(text=text, fg=color)

    @staticmethod
    def _short_name(name: str, limit: int = 34) -> str:
        if len(name) <= limit:
            return name
        return name[:16] + "..." + name[-(limit - 19):]

    def _save_cfg(self):
        core.save_config({"output_dir": self.output_dir, "view": self.view})

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_q.get_nowait()
                kind = msg[0]
                if kind == "fetchstart":
                    self.set_status(msg[2], MUTED)
                elif kind == "meta":
                    _, _url, items = msg
                    for it in items:
                        self.items.append(it)
                        self.item_map[it.key] = it
                        self.sel_vars[it.key] = tk.IntVar(value=1)
                elif kind == "fetcherr":
                    _, url, err = msg
                    short = url if len(url) < 40 else url[:37] + "..."
                    self.set_status(f"{short}: {err}", RED)
                    if not self.items:
                        messagebox.showwarning(core.APP_NAME, err, parent=self)
                elif kind == "fetchdone":
                    self.fetching = False
                    self.search_running = False
                    self.fetch_btn.configure(state="normal", text="Fetch videos")
                    ok_links, fail_links, total_links = msg[2] if msg[2] else (0, 0, 0)
                    if self.items:
                        self._clear_results()
                        self.render_cards()
                        self._start_thumbs()
                        opts = ["Best available"] + core.quality_options(self.items)
                        prev = self.quality_box.get()
                        self.quality_box.configure(values=opts)
                        self.quality_box.set(
                            prev if prev in opts else "Best available"
                        )
                        self._update_counter()
                        summary = (
                            f"{len(self.items)} video"
                            f"{'s' if len(self.items) != 1 else ''} found from "
                            f"{ok_links}/{total_links} link"
                            f"{'s' if total_links != 1 else ''}"
                        )
                        if fail_links:
                            summary += (
                                f" ({fail_links} link"
                                f"{'s' if fail_links != 1 else ''} failed - see above)"
                            )
                        summary += " - pick and press Download selected"
                        self.set_status(summary, GREEN if not fail_links else AMBER)
                elif kind == "thumb":
                    _, key, data = msg
                    try:
                        img = Image.open(BytesIO(data))
                        if self.view == "grid":
                            size = (GRID_CARD_W, GRID_THUMB_H)
                        else:
                            size = (LIST_THUMB_W, LIST_THUMB_H)
                        img = img.convert("RGB").resize(size, Image.LANCZOS)
                        ph = ImageTk.PhotoImage(img)
                        self.photos[key] = ph
                        lbl = self.thumb_label.get(key)
                        if lbl is not None:
                            lbl.configure(image=ph)
                            lbl.image = ph
                    except Exception:
                        pass
                elif kind == "progress":
                    _, key, done, total = msg
                    pct = f" · {int(done * 100 / total)}%" if total else ""
                    size = core.human_size(total) if total else core.human_size(done)
                    self.set_item_status(key, f"Downloading{pct} · "
                                              f"{core.human_size(done)} / {size}",
                                         ACCENT)
                elif kind == "done":
                    _, key, path = msg
                    self._dl_done += 1
                    self._dl_ok += 1
                    self.progress.configure(value=self._dl_done)
                    name = self._short_name(os.path.basename(path))
                    self.set_item_status(key, f"Saved - {name}", GREEN)
                elif kind == "fail":
                    _, key, err = msg
                    self._dl_done += 1
                    self._dl_fail += 1
                    self.progress.configure(value=self._dl_done)
                    self.set_item_status(key, f"Failed - {err}", RED)
                elif kind == "alldone":
                    self.downloading = False
                    self.cancel_btn.configure(state="disabled")
                    for w in (self.download_btn, self.fetch_btn):
                        w.configure(state="normal")
                    self.open_btn.configure(state="normal")
                    if self.cancel_event.is_set():
                        self.set_status(
                            f"Cancelled - {self._dl_ok} saved, "
                            f"{self._dl_fail} failed", AMBER
                        )
                    else:
                        self.set_status(
                            f"Done - {self._dl_ok} saved"
                            + (f", {self._dl_fail} failed" if self._dl_fail else "")
                            + f"  ({self.output_dir})", GREEN if not self._dl_fail
                            else AMBER
                        )
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def destroy(self):
        self.cancel_event.set()
        try:
            self._save_cfg()
        except Exception:
            pass
        super().destroy()


def main() -> None:
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("XGrab.TCombobox", fieldbackground=SURFACE, background=CARD,
                    foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                    lightcolor=SURFACE, darkcolor=SURFACE)
    style.map("XGrab.TCombobox", fieldbackground=[("readonly", SURFACE)],
              foreground=[("readonly", TEXT)])
    style.configure("XGrab.Horizontal.TProgressbar", troughcolor=SURFACE,
                    background=ACCENT, bordercolor=SURFACE, lightcolor=ACCENT,
                    darkcolor=ACCENT)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
