"""Offline tests for the XVidGrab Desktop search collector (no network, no
browser).  The selenium-driven scroll loop itself needs Edge, so these tests
cover the pure parts: search-URL detection, the harvest-merge logic and the
resolution chunking (with fetch_tweet monkeypatched)."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xgrab_core as core  # noqa: E402
import xgrab_search as xsearch  # noqa: E402


# --------------------------------------------------------------- URL detect
class TestIsSearchUrl:
    def test_owner_report_url(self):
        # the exact link from the owner report
        assert (
            xsearch.is_search_url(
                "https://x.com/search?q=backshots&src=recent_search_click"
            )
            == "https://x.com/search?q=backshots&src=recent_search_click"
        )

    def test_bare_domain_and_no_scheme(self):
        assert xsearch.is_search_url("x.com/search?q=cats") == "https://x.com/search?q=cats"
        assert xsearch.is_search_url("twitter.com/search?q=cats") == (
            "https://x.com/search?q=cats"
        )

    def test_variants(self):
        assert xsearch.is_search_url("https://www.x.com/search?q=a&f=live")
        assert xsearch.is_search_url("https://mobile.twitter.com/search?q=a")
        assert xsearch.is_search_url("https://x.com/i/search?q=a")
        assert xsearch.is_search_url("https://x.com/hashtag/cats")
        assert xsearch.is_search_url("https://x.com/explore")

    def test_embedded_in_text(self):
        text = "check this https://x.com/search?q=backshots&src=recent_search_click thanks"
        assert xsearch.is_search_url(text) == (
            "https://x.com/search?q=backshots&src=recent_search_click"
        )

    def test_i_search_normalised(self):
        assert xsearch.is_search_url("https://x.com/i/search?q=a") == (
            "https://x.com/search?q=a"
        )

    def test_status_links_are_not_search(self):
        assert xsearch.is_search_url(
            "https://x.com/SpaceX/status/2122355344576037338"
        ) is None

    def test_other_domains_rejected(self):
        assert xsearch.is_search_url("https://github.com/search?q=x") is None
        assert xsearch.is_search_url("https://example.com/hashtag/a") is None

    def test_empty(self):
        assert xsearch.is_search_url("") is None
        assert xsearch.is_search_url(None) is None


# ----------------------------------------------------------------- JS lint
class TestCollectorJs:
    def test_collect_js_has_extension_v120_shapes(self):
        # the two shapes the extension v1.2.0 fix taught the scanner about
        assert "videoComponent" in xsearch.COLLECT_JS
        assert "playButton" in xsearch.COLLECT_JS
        assert "/video/" in xsearch.COLLECT_JS          # unplayed-tile fallback
        assert "article[data-testid='tweet']" in xsearch.COLLECT_JS
        assert "scrollHeight" in xsearch.STATE_JS

    def test_collect_js_balanced(self):
        js = xsearch.COLLECT_JS
        assert js.count("{") == js.count("}")
        assert js.count("(") == js.count(")")
        assert js.count("[") == js.count("]")


# ------------------------------------------------------------- harvest merge
class TestHarvestMerge:
    def _collector(self):
        return xsearch.SearchCollector("https://x.com/search?q=test")

    def _fake_driver(self, payload):
        class FakeDriver:
            def execute_script(self, _script):
                return payload

        return FakeDriver()

    def test_merges_and_dedupes(self):
        c = self._collector()
        first = {
            "n": 2,
            "h": 5000,
            "login": False,
            "items": [
                {"id": "111", "user": "alice", "text": "a"},
                {"id": "222", "user": "bob", "text": "b"},
            ],
        }
        again = {
            "n": 1,
            "h": 9000,
            "login": False,
            "items": [
                {"id": "111", "user": "alice", "text": "a"},   # still mounted
                {"id": "333", "user": "carol", "text": "c"},   # new
            ],
        }
        c._harvest(self._fake_driver(first))
        c._harvest(self._fake_driver(again))
        assert list(c.found.keys()) == ["111", "222", "333"]
        assert c.found["333"]["user"] == "carol"

    def test_skips_non_numeric_ids(self):
        c = self._collector()
        payload = {"n": 1, "h": 1, "login": False,
                   "items": [{"id": "abc", "user": "x", "text": ""}]}
        assert c._harvest(self._fake_driver(payload)) == (1, 1)
        assert c.found == {}

    def test_bad_json_returns_empty(self):
        c = self._collector()

        class BadDriver:
            def execute_script(self, _s):
                return "not json {{"

        assert c._harvest(BadDriver()) == (0, 0)
        assert c.found == {}


# ------------------------------------------------------------ resolve chunk
class TestResolveAll:
    def test_chunks_and_skips_failed_lookups(self, monkeypatch):
        c = xsearch.SearchCollector("https://x.com/search?q=t")
        c.found = {str(i): {"user": "u", "text": ""} for i in range(25)}

        # 25 posts: evens resolve to one video, odds fail (deleted posts)
        def fake_fetch(tid):
            if int(tid) % 2:
                raise core.XGrabError("Post not found - it may be deleted.")
            return {
                "user": {"screen_name": "u" + tid},
                "mediaDetails": [
                    {
                        "type": "video",
                        "media_url_https": "https://pbs.twimg.com/x.jpg",
                        "video_info": {
                            "duration_millis": 1000,
                            "variants": [
                                {
                                    "bitrate": 100,
                                    "content_type": "video/mp4",
                                    "url": "https://video.twimg.com/x/720x1280/a.mp4",
                                }
                            ],
                        },
                    }
                ],
            }

        monkeypatch.setattr(core, "fetch_tweet", fake_fetch)
        monkeypatch.setattr(xsearch, "RESOLVE_DELAY", 0)  # fast test

        got_chunks: list[list[core.VideoItem]] = []
        c.on_items = got_chunks.append
        msgs: list[str] = []
        c.progress = msgs.append

        items = c._resolve_all()
        assert len(items) == 13          # 0,2,4,...,24
        assert all(isinstance(it, core.VideoItem) for it in items)
        assert items[0].uploader == "u0"
        assert items[0].quality_label == "1280p"
        # chunking: 13 items -> 10 + 3
        assert [len(ch) for ch in got_chunks] == [10, 3]
        assert any("Done" in m for m in msgs)

    def test_stop_event_ends_early(self, monkeypatch):
        c = xsearch.SearchCollector("https://x.com/search?q=t")
        c.found = {str(i): {"user": "u", "text": ""} for i in range(50)}
        stop = threading.Event()
        c.stop_event = stop
        monkeypatch.setattr(xsearch, "RESOLVE_DELAY", 0)

        seen = {"n": 0}

        def fake_fetch(tid):
            seen["n"] += 1
            if seen["n"] >= 5:
                stop.set()
            return {"user": {"screen_name": "u"}, "mediaDetails": []}

        monkeypatch.setattr(core, "fetch_tweet", fake_fetch)
        items = c._resolve_all()
        assert seen["n"] <= 6            # stopped soon after the flag went up
        assert items == []
