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

import http.client
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

__version__ = "1.2.1"
APP_NAME = "XVidGrab Desktop"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SYNDICATION_ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"
HTTP_TIMEOUT = 20
MAX_TCO_EXPANSIONS = 25
# Pasting many links at once (a thread, a saved list) fetches each post
# sequentially from the same unauthenticated syndication endpoint; firing
# them back-to-back with no gap reliably trips its rate limit after only a
# handful of requests, silently failing the rest. A delay between requests
# in a batch keeps it well under that limit; a short in-call retry (below)
# absorbs one-off blips, and fetch_videos_batch's cooldown passes recover
# anything still rate-limited after that.
BATCH_FETCH_DELAY = 1.0
# Transient 429/403 responses (the endpoint is momentarily rate-limiting)
# are retried with backoff before being treated as a real failure.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_BASE = 1.5
# fetch_videos_batch: links still rate-limited after their in-call retries
# are collected and retried as additional passes once the whole list has
# been through, after a cooldown that grows each pass - most unauthenticated
# rate limits are a rolling window, so waiting it out recovers them.
BATCH_MAX_PASSES = 5
BATCH_COOLDOWN_START = 20.0
BATCH_COOLDOWN_GROWTH = 1.6
BATCH_COOLDOWN_MAX = 120.0
BATCH_DELAY_GROWTH = 1.5
BATCH_DELAY_MAX = 4.0

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


class RateLimitError(XGrabError):
    """A 429/403 from the syndication endpoint that survived the in-call
    retries. Distinct from a hard failure (deleted post, bad link) so
    fetch_videos_batch can retry it in a later cooldown pass instead of
    giving up on it."""


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


def _http_get(url: str, retries: int = 3) -> bytes:
    """GET *url* with automatic retries for flaky connections.

    X's CDN occasionally drops chunked responses mid-transfer
    (``http.client.IncompleteRead`` / ``RemoteDisconnected``), which used to
    surface as a raw "Unexpected error" dialog.  Real HTTP status errors are
    never retried - those are translated by the caller.
    """
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            raise  # a real status code - caller turns it into a friendly error
        except (http.client.HTTPException, OSError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 5.0))
    raise last_exc  # type: ignore[misc]


def fetch_tweet(tweet_id: str) -> dict:
    """Fetch the public syndication JSON for one post id.

    Retries a transient 429/403 (rate limit) a few times with backoff
    before giving up - in a large batch these are usually momentary, not
    a hard block."""
    url = _syndication_url(tweet_id)
    raw = None
    last_exc: urllib.error.HTTPError | None = None
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            raw = _http_get(url)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise XGrabError(
                    "Post not found - it may be deleted, private, or the link is wrong."
                ) from exc
            if exc.code in (403, 429):
                last_exc = exc
                if attempt < RATE_LIMIT_RETRIES:
                    time.sleep(RATE_LIMIT_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise RateLimitError(
                    "X is temporarily limiting requests. Wait a minute and try again."
                ) from exc
            raise XGrabError(f"X returned HTTP {exc.code} for that post.") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise XGrabError(
                "Could not reach X - check your internet connection and try again."
            ) from exc
    if raw is None:
        raise RateLimitError(
            "X is temporarily limiting requests. Wait a minute and try again."
        ) from last_exc
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
    """Turn a syndication JSON payload into a list of VideoItem.

    A quote tweet has no media of its own - the video lives on the tweet
    it quotes, exposed here under "quoted_tweet"/"quoted_status". Search's
    scroll-harvest can pick up the outer (quoting) tweet's id when a video
    shows inside its embedded quote card, so without this fallback a
    perfectly valid video post reports "No videos found in that post".
    """
    source = data
    media = data.get("mediaDetails") or data.get("media_details") or []
    if not media:
        quoted = (
            data.get("quoted_tweet")
            or data.get("quotedTweet")
            or data.get("quoted_status")
            or {}
        )
        q_media = quoted.get("mediaDetails") or quoted.get("media_details") or []
        if q_media:
            source = quoted
            media = q_media
    video_media = [
        m
        for m in media
        if m.get("type") in ("video", "animated_gif") and m.get("video_info")
    ]
    total = len(video_media)
    user = (source.get("user") or {}).get("screen_name") or "x"
    text = data.get("text") or source.get("text") or ""
    # when sourced from a quoted tweet, link to ITS id - the outer tweet_id
    # has no video of its own, so a link built from it wouldn't show one
    link_id = str(source.get("id_str") or source.get("id") or "") or tweet_id
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
                webpage_url=f"https://x.com/{user}/status/{link_id}"
                + (f"/video/{i}" if total > 1 else ""),
            )
        )
    return items


def _dump_debug_json(tweet_id: str, data: dict) -> str | None:
    """Save the raw syndication JSON for a "no videos found" post.

    Only runs when XVIDGRAB_DEBUG is set in the environment - this is for
    diagnosing a specific false "no videos" report without logging
    anything by default. Returns the saved path, or None on any failure
    (debug dumping must never break a real fetch).
    """
    if not os.environ.get("XVIDGRAB_DEBUG"):
        return None
    try:
        debug_dir = os.path.join(os.path.expanduser("~"), ".xvidgrab-desktop-debug")
        os.makedirs(debug_dir, exist_ok=True)
        path = os.path.join(debug_dir, f"{tweet_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return path
    except Exception:
        return None


def fetch_videos(url: str) -> list[VideoItem]:
    """Fetch every video of one canonical post URL. Raises XGrabError."""
    m = _STATUS_RE.search(url or "")
    if not m:
        raise XGrabError("That does not look like an X post link.")
    tweet_id = m.group(2)
    data = fetch_tweet(tweet_id)
    items = parse_videos(data, tweet_id)
    if not items:
        dumped = _dump_debug_json(tweet_id, data)
        msg = "No videos found in that post - it may contain only photos."
        if dumped:
            msg += f"\n(debug JSON saved: {dumped})"
        raise XGrabError(msg)
    return items


def fetch_videos_batch(
    urls: list[str],
    on_item=None,
    on_error=None,
    stop_event=None,
) -> tuple[int, int]:
    """Fetch every link in *urls*, robust to a large batch tripping the
    syndication endpoint's rate limit.

    Each pass walks whatever is still pending, pacing requests with a delay
    that grows the moment a rate limit is actually hit (most batches never
    need it to grow at all). Links still rate-limited at the end of a pass
    are retried in a further pass after a cooldown, which also grows each
    time - unauthenticated rate limits are almost always a rolling window,
    so waiting it out recovers links that a plain retry-on-the-spot can't.
    A link that fails for a REAL reason (deleted, private, no videos) is
    not retried - only rate-limited ones are.

    on_item(items), if given, is called with each link's VideoItem list as
    soon as it resolves. on_error(url, message) is called for every
    failure (url is "" for a batch-level progress note, e.g. the cooldown
    announcement). stop_event, if given and set, stops the batch early
    (whatever hasn't been attempted yet is left out of the counts).

    Returns (ok_count, fail_count) covering every url in *urls* exactly
    once each.
    """
    pending = list(dict.fromkeys(urls))  # de-dup, keep order
    ok_count = 0
    hard_fail_count = 0
    delay = BATCH_FETCH_DELAY
    cooldown = BATCH_COOLDOWN_START

    for pass_num in range(BATCH_MAX_PASSES):
        if not pending:
            break
        rate_limited: list[str] = []
        for i, u in enumerate(pending):
            if stop_event is not None and stop_event.is_set():
                return ok_count, hard_fail_count + len(pending) - i
            if i > 0:
                time.sleep(delay)
            try:
                items = fetch_videos(u)
                if on_item:
                    on_item(items)
                ok_count += 1
            except RateLimitError:
                rate_limited.append(u)
                delay = min(delay * BATCH_DELAY_GROWTH, BATCH_DELAY_MAX)
            except XGrabError as exc:
                hard_fail_count += 1
                if on_error:
                    on_error(u, str(exc))
            except Exception as exc:  # defensive
                hard_fail_count += 1
                if on_error:
                    on_error(u, f"Unexpected error: {exc}")
        pending = rate_limited
        if pending and pass_num < BATCH_MAX_PASSES - 1:
            if stop_event is not None and stop_event.is_set():
                return ok_count, hard_fail_count + len(pending)
            if on_error:
                on_error(
                    "",
                    f"{len(pending)} link(s) still rate-limited - "
                    f"cooling down {int(cooldown)}s before retrying...",
                )
            time.sleep(cooldown)
            cooldown = min(cooldown * BATCH_COOLDOWN_GROWTH, BATCH_COOLDOWN_MAX)

    hard_fail_count += len(pending)  # exhausted all passes, still limited
    return ok_count, hard_fail_count


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


DOWNLOAD_RETRIES = 5          # extra attempts after the first one
DOWNLOAD_BACKOFF_CAP = 15.0   # seconds between attempts (exponential, capped)
DOWNLOAD_CHUNK_SIZE = 262144  # 256 KB reads keep big videos flowing


class _RetryDownload(Exception):
    """Internal: transfer ended early - caller should retry (and resume)."""


class _CancelledDownload(Exception):
    """Internal: the user pressed Cancel."""


def _discard_part(part_path: str, meta_path: str) -> None:
    for path in (part_path, meta_path):
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def _probe_size(url: str) -> int | None:
    """Best-effort full size of *url* via HEAD (None when unknown)."""
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            total = resp.headers.get("Content-Length")
            return int(total) if total and total.isdigit() else None
    except Exception:
        return None


def _download_attempt(
    url: str,
    dest_path: str,
    part_path: str,
    meta_path: str,
    progress_cb,
    cancel_event: threading.Event | None,
    chunk_size: int,
) -> None:
    """One download pass; resumes a matching ``.part`` file.

    Raises ``_RetryDownload`` when the transfer ends early so the caller can
    retry - the bytes already on disk are kept and the next pass continues
    from that offset via a ``Range`` request.
    """
    base = 0
    resume = False
    if os.path.exists(part_path) and os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            if meta.get("url") == url:
                base = os.path.getsize(part_path)
                resume = base > 0
        except (OSError, ValueError):
            resume = False

    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if resume:
        headers["Range"] = f"bytes={base}-"

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and resume:
            # The .part file is already at (or past) the full size - this
            # happens when a previous pass finished writing but crashed
            # before the atomic rename.  Verify and finish it.
            full = _probe_size(url)
            if full == base:
                os.replace(part_path, dest_path)
                _discard_part("", meta_path)
                return
            _discard_part(part_path, meta_path)  # stale/corrupt part - start over
            raise _RetryDownload() from exc
        raise

    with resp:
        status = getattr(resp, "status", None) or 200
        if resume and status != 206:
            # Server ignored the Range header - restart from scratch.
            resume = False
            base = 0

        hdr = resp.headers
        expected = hdr.get("Content-Length")
        expected = int(expected) if expected and expected.isdigit() else None
        full_total: int | None = None
        if status == 206:
            m = re.search(r"/(\d+)\s*$", (hdr.get("Content-Range") or "").strip())
            if m:
                full_total = int(m.group(1))  # total size of the whole file
        elif not resume and expected is not None:
            full_total = expected

        if status == 206 and full_total is not None and base >= full_total:
            if base == full_total:
                os.replace(part_path, dest_path)
                _discard_part("", meta_path)
                return
            _discard_part(part_path, meta_path)  # corrupt part - start over
            raise _RetryDownload()

        if not resume:
            # Tag the fresh .part with its stream so later passes (and later
            # app runs) can resume it safely.
            try:
                with open(meta_path, "w", encoding="utf-8") as fh:
                    json.dump({"url": url, "saved": time.time()}, fh)
            except OSError:
                pass

        if progress_cb:
            try:
                progress_cb(base, full_total)
            except Exception:
                pass

        mode = "ab" if resume else "wb"
        with open(part_path, mode) as fh:
            downloaded = 0
            last_cb = 0.0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise _CancelledDownload()
                try:
                    chunk = resp.read(chunk_size)
                except http.client.IncompleteRead as exc:
                    # Server cut the chunked response short.  Keep whatever
                    # arrived, then retry - the next pass resumes from here.
                    if exc.partial:
                        try:
                            fh.write(exc.partial)
                        except OSError:
                            pass
                    raise _RetryDownload() from exc
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if progress_cb and now - last_cb >= 0.25:
                    try:
                        progress_cb(base + downloaded, full_total)
                    except Exception:
                        pass
                    last_cb = now

        done = base + downloaded
        if full_total is not None and done < full_total:
            raise _RetryDownload()  # short file - retry and resume
        os.replace(part_path, dest_path)
        _discard_part("", meta_path)


def download_file(
    url: str,
    dest_path: str,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    chunk_size: int = DOWNLOAD_CHUNK_SIZE,
) -> str:
    """Stream *url* to *dest_path* (atomic ``.part`` rename).

    Hardened against X's CDN dropping connections mid-transfer
    (``IncompleteRead``, ``ChunkedEncodingError``-style cuts, resets):
    up to ``DOWNLOAD_RETRIES`` extra attempts with exponential backoff,
    byte-range resume from the ``.part`` file, ``Accept-Encoding: identity``
    so byte counts match the wire, and a final size check against
    Content-Length before the file is accepted.
    Returns *dest_path*.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    part_path = dest_path + ".part"
    meta_path = dest_path + ".part.meta"

    attempts = DOWNLOAD_RETRIES + 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            _download_attempt(
                url, dest_path, part_path, meta_path,
                progress_cb, cancel_event, chunk_size,
            )
            return dest_path
        except _CancelledDownload:
            # Keep .part + meta so the next click resumes where it stopped.
            raise XGrabError("Cancelled")
        except urllib.error.HTTPError as exc:
            raise XGrabError(f"Download failed (HTTP {exc.code}).") from exc
        except _RetryDownload as exc:
            last_exc = exc
        except (http.client.HTTPException, OSError) as exc:
            last_exc = exc
        if attempt < attempts - 1:
            delay = min(2 ** attempt, DOWNLOAD_BACKOFF_CAP)
            if cancel_event is not None and cancel_event.wait(delay):
                raise XGrabError("Cancelled")
    raise XGrabError(
        f"Download kept failing after {attempts} attempts "
        f"({last_exc}). Your connection to X's CDN kept dropping - "
        "try again and it resumes from where it stopped."
    )


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
