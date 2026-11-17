"""XVidGrab Desktop - shared engine (no GUI imports).

Paste one or more X (Twitter) post links, list every video found on the
page with thumbnails and available resolutions, and download the selected
ones at the highest quality available (or a chosen cap).

Engine notes
------------
Video discovery uses X's public syndication endpoint
(cdn.syndication.twimg.com/tweet-result), the same public data source the
official embed widgets use. No account, no cookies, no API keys.
Multi-video posts (up to 4 items) come back in one call; animated GIFs are
treated as looping MP4 videos.

Developed by www.3SVerse.com - part of the 3SVerse suite.
MIT licensed - see LICENSE.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

__version__ = "1.1.0"
APP_NAME = "XVidGrab Desktop"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SYNDICATION_ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"
HTTP_TIMEOUT = 20
MAX_TCO_EXPANSIONS = 25

# x.com/<user>/status/<id>  (accepts twitter.com, www., mobile., an optional
# leading @ on the handle, and trailing /photo/N, /video/N, query strings and
# #fragments)
_STATUS_RE = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.)?(?:twitter|x)\.com/?@?"
    r"([A-Za-z0-9_]{1,20})/status(?:es)?/(\d{1,25})",
    re.IGNORECASE,
)
_TCO_RE = re.compile(r"(?:https?://)?t\.co/([A-Za-z0-9]+)", re.IGNORECASE)
_DIMS_RE = re.compile(r"/(\d{2,5})x(\d{2,5})(?:/|$|\?)")


class XGrabError(Exception):
    """User-facing error with a friendly message."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class VideoFormat:
    content_type: str
    width: int
    height: int
    bitrate: int
    url: str

    @property
    def label(self) -> str:
        return f"{self.height}p" if self.height else "video"


@dataclass
class VideoItem:
    tweet_id: str
    index: int  # 1-based position inside the post
    total: int  # how many videos the post has
    uploader: str  # screen name without @
    text: str  # post text
    duration_ms: int | None
    media_type: str  # "video" | "animated_gif"
    thumbnail_url: str
    formats: list[VideoFormat] = field(default_factory=list)
    webpage_url: str = ""

    @property
    def key(self) -> tuple[str, int]:
        return (self.tweet_id, self.index)

    @property
    def title(self) -> str:
        t = " ".join(self.text.split())
        if t:
            t = t.split("http")[0].strip()  # drop trailing t.co links
            if len(t) > 90:
                t = t[:87].rstrip() + "..."
        if not t:
            t = f"Video {self.index} of {self.total}" if self.total > 1 else "X video"
        return t

    @property
    def best(self) -> VideoFormat | None:
        return self.formats[0] if self.formats else None

    @property
    def quality_label(self) -> str:
        b = self.best
        return b.label if b else "?"

    @property
    def duration_label(self) -> str:
        return format_duration(self.duration_ms)


# --------------------------------------------------------------------------
# URL parsing / normalisation
# --------------------------------------------------------------------------
def parse_status_urls(text: str) -> list[str]:
    """Extract every x.com/twitter.com status URL from *text*.

    Returns canonical ``https://x.com/<user>/status/<id>`` links,
    de-duplicated, order preserved.
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for match in _STATUS_RE.finditer(text):
        user, status_id = match.group(1), match.group(2)
        canonical = f"https://x.com/{user}/status/{status_id}"
        if canonical.lower() not in seen:
            seen.add(canonical.lower())
            out.append(canonical)
    return out


def expand_tco_links(text: str) -> str:
    """Resolve t.co short links inline so they can be parsed as status URLs.

    Only expands up to MAX_TCO_EXPANSIONS links; failures are left as-is.
    """
    links = _TCO_RE.findall(text or "")
    if not links:
        return text
    for i, slug in enumerate(links[:MAX_TCO_EXPANSIONS]):
        try:
            req = urllib.request.Request(
                f"https://t.co/{slug}",
                method="HEAD",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                final = resp.geturl()
            if final and final != f"https://t.co/{slug}":
                text = text.replace(f"https://t.co/{slug}", final).replace(
                    f"t.co/{slug}", final
                )
        except Exception:
            continue
    return text


# --------------------------------------------------------------------------
# Syndication API
# --------------------------------------------------------------------------
def _syndication_url(tweet_id: str) -> str:
    qs = urllib.parse.urlencode({"id": tweet_id, "token": "x", "lang": "en"})
    return f"{SYNDICATION_ENDPOINT}?{qs}"


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def fetch_tweet(tweet_id: str) -> dict:
    """Fetch the public syndication JSON for one post id."""
    url = _syndication_url(tweet_id)
    try:
        raw = _http_get(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise XGrabError(
                "Post not found - it may be deleted, private, or the link is wrong."
            ) from exc
        if exc.code in (403, 429):
            raise XGrabError(
                "X is temporarily limiting requests. Wait a minute and try again."
            ) from exc
        raise XGrabError(f"X returned HTTP {exc.code} for that post.") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise XGrabError(
            "Could not reach X - check your internet connection and try again."
        ) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise XGrabError(
            "Unexpected response from X - the extractor may need an update."
        ) from exc


def _dims_from_url(url: str) -> tuple[int, int]:
    m = _DIMS_RE.search(url)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


def parse_videos(data: dict, tweet_id: str) -> list[VideoItem]:
    """Turn a syndication JSON payload into a list of VideoItem."""
    media = data.get("mediaDetails") or data.get("media_details") or []
    video_media = [
        m
        for m in media
        if m.get("type") in ("video", "animated_gif") and m.get("video_info")
    ]
    total = len(video_media)
    user = (data.get("user") or {}).get("screen_name") or "x"
    text = data.get("text") or ""
    items: list[VideoItem] = []
    for i, m in enumerate(video_media, start=1):
        variants = (m.get("video_info") or {}).get("variants") or []
        fmts: list[VideoFormat] = []
        for v in variants:
            url = v.get("url") or ""
            ctype = v.get("content_type") or ""
            if ctype != "video/mp4" or not url:
                continue  # skip HLS m3u8; direct MP4s are always present
            w, h = _dims_from_url(url)
            fmts.append(
                VideoFormat(
                    content_type=ctype,
                    width=w,
                    height=h,
                    bitrate=int(v.get("bitrate") or 0),
                    url=url,
                )
            )
        # de-duplicate by resolution (keep highest bitrate), best first
        by_res: dict[tuple[int, int], VideoFormat] = {}
        for f in fmts:
            res = (f.width, f.height)
            if res not in by_res or f.bitrate > by_res[res].bitrate:
                by_res[res] = f
        fmts = sorted(
            by_res.values(), key=lambda f: (f.height * f.width, f.bitrate), reverse=True
        )
        items.append(
            VideoItem(
                tweet_id=tweet_id,
                index=i,
                total=total,
                uploader=user,
                text=text,
                duration_ms=(m.get("video_info") or {}).get("duration_millis"),
                media_type=m.get("type") or "video",
                thumbnail_url=m.get("media_url_https") or "",
                formats=fmts,
                webpage_url=f"https://x.com/{user}/status/{tweet_id}"
                + (f"/video/{i}" if total > 1 else ""),
            )
        )
    return items


def fetch_videos(url: str) -> list[VideoItem]:
    """Fetch every video of one canonical post URL. Raises XGrabError."""
    m = _STATUS_RE.search(url or "")
    if not m:
        raise XGrabError("That does not look like an X post link.")
    tweet_id = m.group(2)
    data = fetch_tweet(tweet_id)
    items = parse_videos(data, tweet_id)
    if not items:
        raise XGrabError(
            "No videos found in that post - it may contain only photos."
        )
    return items


# --------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------
def thumbnail_url_for(item: VideoItem, size: str = "small") -> str:
    """Return a bandwidth-friendly thumbnail URL (pbs.twimg.com name param)."""
    url = item.thumbnail_url
    if not url:
        return ""
    if "pbs.twimg.com" in url and "name=" not in url:
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lstrip(".") or "jpg"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}format={ext}&name={size}"
    return url


def fetch_thumbnail_bytes(item: VideoItem, size: str = "small") -> bytes:
    """Download thumbnail bytes; falls back to the bare URL on failure."""
    url = thumbnail_url_for(item, size)
    if not url:
        raise XGrabError("No thumbnail for this video.")
    try:
        return _http_get(url)
    except Exception:
        if url != item.thumbnail_url and item.thumbnail_url:
            return _http_get(item.thumbnail_url)
        raise


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------
def pick_format(item: VideoItem, max_height: int | None = None) -> VideoFormat:
    """Choose the best format, optionally capped at *max_height* pixels.

    If nothing fits under the cap, the smallest available format is used
    (a cap is a bandwidth preference, so never exceed it).
    """
    if not item.formats:
        raise XGrabError("No downloadable MP4 stream was found for this video.")
    fmts = [f for f in item.formats if f.height > 0] or list(item.formats)
    if max_height:
        capped = [f for f in fmts if f.height <= max_height]
        if capped:
            fmts = capped
        else:
            fmts = list(reversed(fmts))  # smallest first -> take the lowest
    return fmts[0]


def download_file(
    url: str,
    dest_path: str,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    chunk_size: int = 65536,
) -> str:
    """Stream *url* to *dest_path* (atomic .part rename). Returns dest_path."""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            part_path = dest_path + ".part"
            downloaded = 0
            last_cb = 0.0

            with open(part_path, "wb") as fh:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        fh.close()
                        try:
                            os.remove(part_path)
                        except OSError:
                            pass
                        raise XGrabError("Cancelled")
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if progress_cb and (now - last_cb >= 0.25 or downloaded == total):
                        try:
                            progress_cb(downloaded, total)
                        except Exception:
                            pass
                        last_cb = now
            os.replace(part_path, dest_path)
    except urllib.error.HTTPError as exc:
        raise XGrabError(f"Download failed (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, XGrabError):
            raise
        raise XGrabError(f"Download failed: {exc}") from exc
    return dest_path


# --------------------------------------------------------------------------
# Names / helpers
# --------------------------------------------------------------------------
_UNSAFE_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = _UNSAFE_FS.sub("", name).strip(" .")
    name = " ".join(name.split())
    if len(name) > max_len:
        name = name[: max_len - 3].rstrip() + "..."
    return name or "video"


def build_filename(item: VideoItem, fmt: VideoFormat | None = None) -> str:
    fmt = fmt or item.best
    quality = fmt.label if fmt else "video"
    parts = [item.uploader, item.tweet_id]
    if item.total > 1:
        parts.append(str(item.index))
    parts.append(quality)
    return sanitize_filename("-".join(parts)) + ".mp4"


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for n in range(1, 1000):
        candidate = f"{stem} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return path


def format_duration(duration_ms: int | None) -> str:
    if not duration_ms or duration_ms < 0:
        return ""
    secs = int(round(duration_ms / 1000))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(num: float | None) -> str:
    if not num:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} GB"


def quality_options(items: list[VideoItem]) -> list[str]:
    """Union of available heights across items, e.g. ['1080p', '720p', ...]."""
    heights = sorted({f.height for it in items for f in it.formats if f.height > 0},
                     reverse=True)
    return [f"{h}p" for h in heights]


def default_download_dir() -> str:
    base = os.path.join(os.path.expanduser("~"), "Downloads")
    if not os.path.isdir(base):  # non-Windows fallback
        base = os.path.expanduser("~")
    return os.path.join(base, "XVidGrab")


def resource_path(rel: str) -> str:
    """Path to a bundled resource, PyInstaller-frozen aware."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


CONFIG_NAME = ".xvidgrab-desktop.json"


def config_path() -> str:
    return os.path.join(os.path.expanduser("~"), CONFIG_NAME)


def load_config() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception:
        pass
