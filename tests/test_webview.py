"""Offline tests for the in-app webview collector (no network, no display,
no browser).  The dialog itself needs QtWebEngine + a real window, so these
tests cover the pure parts: user-agent cleanup, harvest parsing/merging, the
module contract when QtWebEngine is missing, and the GUI wiring (checked by
reading the sources - importing the GUI module is not needed for that)."""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import xgrab_search as xsearch  # noqa: E402
import xgrab_webview as xweb  # noqa: E402


# ------------------------------------------------------------ user agent
class TestCleanUserAgent:
    def test_strips_qt_token_keeps_chrome(self):
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) QtWebEngine/6.8.1 Chrome/131.0.0.0 "
            "Safari/537.36"
        )
        out = xweb.clean_user_agent(ua)
        assert "QtWebEngine" not in out
        assert "Chrome/131.0.0.0" in out
        assert "Safari/537.36" in out

    def test_normal_ua_untouched(self):
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        )
        assert xweb.clean_user_agent(ua) == ua

    def test_empty_and_weird(self):
        assert xweb.clean_user_agent("") == ""
        assert xweb.clean_user_agent(None) == ""


# ---------------------------------------------------------- harvest parsing
class TestParseHarvest:
    def test_json_string(self):
        raw = json.dumps({"n": 3, "h": 5000, "login": False, "items": []})
        assert xweb.parse_harvest(raw) == {
            "n": 3, "h": 5000, "login": False, "items": [],
        }

    def test_dict_passthrough(self):
        d = {"articles": 4, "login": True}
        assert xweb.parse_harvest(d) is d

    def test_bad_json(self):
        assert xweb.parse_harvest("not json {{") == {}

    def test_none(self):
        assert xweb.parse_harvest(None) == {}

    def test_non_dict_json(self):
        assert xweb.parse_harvest('[1,2,3]') == {}


# ----------------------------------------------------------- harvest merge
class TestMergeHarvest:
    def test_merges_and_dedupes(self):
        found = {}
        first = {
            "n": 2, "h": 5000, "login": False,
            "items": [
                {"id": "111", "user": "alice", "text": "a"},
                {"id": "222", "user": "bob", "text": "b"},
            ],
        }
        again = {
            "n": 1, "h": 9000, "login": False,
            "items": [
                {"id": "111", "user": "alice", "text": "a"},   # still mounted
                {"id": "333", "user": "carol", "text": "c"},   # new
            ],
        }
        assert xweb.merge_harvest(found, first) == (2, 5000)
        assert xweb.merge_harvest(found, again) == (1, 9000)
        assert list(found.keys()) == ["111", "222", "333"]
        assert found["333"]["user"] == "carol"

    def test_skips_non_numeric_ids(self):
        found = {}
        payload = {"n": 1, "h": 1, "login": False,
                   "items": [{"id": "abc", "user": "x", "text": ""}]}
        assert xweb.merge_harvest(found, payload) == (1, 1)
        assert found == {}

    def test_same_semantics_as_selenium_collector(self):
        """The webview merge and the Edge collector merge agree on a payload."""
        payload = {
            "n": 2, "h": 1234, "login": False,
            "items": [
                {"id": "777", "user": "dave", "text": "hi"},
                {"id": "777", "user": "dave", "text": "hi"},
            ],
        }
        webview_found = {}
        xweb.merge_harvest(webview_found, payload)

        class FakeDriver:
            def execute_script(self, _s):
                return json.dumps(payload)

        c = xsearch.SearchCollector("https://x.com/search?q=t")
        c._harvest(FakeDriver())

        assert webview_found == c.found


# -------------------------------------------------------- module contract
class TestModuleContract:
    def test_webengine_available_is_bool(self):
        assert isinstance(xweb.webengine_available(), bool)

    def test_missing_webview_raises(self):
        if xweb.webengine_available():
            pytest.skip("QtWebEngine present - fallback path not reachable")
        with pytest.raises(RuntimeError):
            xweb.run_search_dialog(None, "https://x.com/search?q=x")

    def test_profile_dir_not_the_edge_one(self):
        # the in-app browser must NOT share the legacy Edge profile dir
        assert xweb.WEBVIEW_PROFILE_DIR != xsearch.PROFILE_DIR
        assert "XVidGrab-WebViewProfile" in xweb.WEBVIEW_PROFILE_DIR


# --------------------------------------- authenticated-resolve interceptor
class TestVideoCaptureInterceptor:
    """x.com/i/status/<id> (the only form resolve can navigate to without
    already knowing the author's handle) redirects to the canonical
    x.com/<user>/status/<id> once it resolves - the page's real, final URL
    never equals what resolve originally set. Matching must tolerate that
    redirect (substring-match on the tweet id) or it silently records
    nothing, ever, even on a page that loaded and played correctly."""

    def _interceptor(self):
        if not xweb.webengine_available():
            pytest.skip("QtWebEngine not available in this environment")
        return xweb._VideoCaptureInterceptor()

    class _FakeUrl:
        def __init__(self, s):
            self._s = s

        def toString(self):
            return self._s

    class _FakeInfo:
        def __init__(self, first_party, request_url):
            self._fp = first_party
            self._ru = request_url

        def firstPartyUrl(self):
            return TestVideoCaptureInterceptor._FakeUrl(self._fp)

        def requestUrl(self):
            return TestVideoCaptureInterceptor._FakeUrl(self._ru)

    def test_captures_after_redirect_to_canonical_url(self):
        itc = self._interceptor()
        itc.target_tweet_id = "12345"
        info = self._FakeInfo(
            first_party="https://x.com/someuser/status/12345",
            request_url="https://video.twimg.com/ext_tw_video/12345/pu/vid/avc1/1280x720/x.mp4",
        )
        itc.interceptRequest(info)
        assert itc.found == [
            "https://video.twimg.com/ext_tw_video/12345/pu/vid/avc1/1280x720/x.mp4"
        ]

    def test_ignores_other_pages_on_same_shared_profile(self):
        itc = self._interceptor()
        itc.target_tweet_id = "12345"
        info = self._FakeInfo(
            first_party="https://x.com/search?q=something",
            request_url="https://video.twimg.com/ext_tw_video/99999/pu/vid/avc1/1280x720/x.mp4",
        )
        itc.interceptRequest(info)
        assert itc.found == []

    def test_no_target_records_nothing(self):
        itc = self._interceptor()
        itc.target_tweet_id = ""
        info = self._FakeInfo(
            first_party="https://x.com/someuser/status/12345",
            request_url="https://video.twimg.com/ext_tw_video/12345/pu/vid/avc1/1280x720/x.mp4",
        )
        itc.interceptRequest(info)
        assert itc.found == []

    def test_ignores_non_video_requests(self):
        itc = self._interceptor()
        itc.target_tweet_id = "12345"
        info = self._FakeInfo(
            first_party="https://x.com/someuser/status/12345",
            request_url="https://pbs.twimg.com/profile_images/1/avatar.jpg",
        )
        itc.interceptRequest(info)
        assert itc.found == []


# ---------------------------------------------------------- JS wrapping
class TestIife:
    """runJavaScript evaluates a classic script - a top-level ``return``
    (fine under selenium's execute_script) is a SyntaxError there.  The
    collector JS must be wrapped in an IIFE before it reaches the page."""

    def test_wraps_in_iife(self):
        assert xweb.iife("return 42;") == "(function(){return 42;})()"

    def test_empty(self):
        assert xweb.iife("") == "(function(){})()"

    def test_collector_js_parses_after_wrapping(self):
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            pytest.skip("node not available for a syntax check")
        for name, js in (
            ("state", xsearch.STATE_JS),
            ("collect", xsearch.COLLECT_JS),
        ):
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False
            ) as fh:
                fh.write(xweb.iife(js))
                path = fh.name
            try:
                subprocess.run(
                    [node, "--check", path], check=True, capture_output=True
                )
            except subprocess.CalledProcessError as exc:
                pytest.fail(f"{name} JS does not parse: {exc.stderr[:400]}")
            finally:
                os.unlink(path)

    def test_dialog_source_uses_iife(self):
        src = (ROOT / "xgrab_webview.py").read_text(encoding="utf-8")
        assert "runJavaScript(iife(xsearch.STATE_JS)" in src
        assert "runJavaScript(iife(xsearch.COLLECT_JS)" in src


# ----------------------------------------------------------- GUI wiring
class TestGuiWiring:
    """Source-lint checks (no Qt import required)."""

    @staticmethod
    def _qt_source() -> str:
        return (ROOT / "xvidgrab_desktop_qt.py").read_text(encoding="utf-8")

    def test_share_opengl_contexts_before_qapplication(self):
        src = self._qt_source()
        attr = src.index("QApplication.setAttribute(Qt.AA_ShareOpenGLContexts")
        app = src.index("app = QApplication(sys.argv)")
        assert attr < app, "AA_ShareOpenGLContexts must be set first"

    def test_uses_in_app_webview_with_edge_fallback(self):
        src = self._qt_source()
        assert "import xgrab_webview as xweb" in src
        assert "xweb.webengine_available()" in src
        # non-modal in-app page window (fetch queue: links join the run)
        assert "xweb.run_page_dialog_async(" in src
        # legacy Edge path only as fallback
        assert "xsearch.SearchCollector(" in src

    def test_spec_bundles_qtwebengine(self):
        spec = (ROOT / "xvidgrab_desktop_qt.spec").read_text(encoding="utf-8")
        assert "PySide6.QtWebEngineWidgets" in spec

    def test_ci_byte_compiles_webview_module(self):
        ci = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        assert "xgrab_webview.py" in ci

    def test_search_resolver_is_shared(self):
        # resolve_ids is the single resolver used by both collectors
        assert hasattr(xsearch, "resolve_ids")
        src = self._qt_source()
        assert "xsearch.resolve_ids(" in src


class TestBuildDmItems:
    """build_dm_items: captured chat CDN URLs become download-list items."""

    def test_groups_variants_of_one_video(self):
        urls = [
            "https://video.twimg.com/dm/1/vid/avc1/1280x720/a.mp4",
            "https://video.twimg.com/dm/1/vid/avc1/480x270/b.mp4",
        ]
        items = xweb.build_dm_items(urls)
        assert len(items) == 1
        assert len(items[0].formats) == 2
        assert items[0].formats[0].height >= items[0].formats[-1].height

    def test_separate_prefixes_are_separate_videos(self):
        urls = [
            "https://video.twimg.com/dm/1/vid/avc1/1280x720/a.mp4",
            "https://video.twimg.com/dm/2/vid/avc1/1280x720/b.mp4",
        ]
        items = xweb.build_dm_items(urls)
        assert len(items) == 2

    def test_posters_attach_in_order(self):
        urls = [
            "https://video.twimg.com/dm/1/vid/avc1/720x720/a.mp4",
            "https://video.twimg.com/dm/2/vid/avc1/720x720/b.mp4",
        ]
        posters = [
            "https://pbs.twimg.com/ext_tw_video_thumb/1/pu/img/x.jpg",
            "https://pbs.twimg.com/ext_tw_video_thumb/2/pu/img/y.jpg",
        ]
        items = xweb.build_dm_items(urls, posters)
        assert items[0].thumbnail_url == posters[0]
        assert items[1].thumbnail_url == posters[1]

    def test_m3u8_ignored(self):
        items = xweb.build_dm_items(
            ["https://video.twimg.com/dm/1/avc1/720x720/a.m3u8"]
        )
        assert items == []
