"""Offline tests for the XVidGrab Desktop core engine (no network)."""

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xgrab_core as core  # noqa: E402


# A trimmed copy of a real syndication API payload: a post with two videos
# (mixed resolutions, one HLS variant that must be skipped).
SYNDICATION_SAMPLE = {
    "id_str": "2122355344576037338",
    "text": "Starship 42 coming in for catch https://t.co/aw0oFsDLbZ",
    "user": {"screen_name": "SpaceX"},
    "mediaDetails": [
        {
            "type": "video",
            "media_url_https": "https://pbs.twimg.com/amplify_video_thumb/111/img/aaa.jpg",
            "video_info": {
                "duration_millis": 19400,
                "variants": [
                    {
                        "bitrate": None,
                        "content_type": "application/x-mpegURL",
                        "url": "https://video.twimg.com/amplify_video/111/pl/xyz.m3u8",
                    },
                    {
                        "bitrate": 256000,
                        "content_type": "video/mp4",
                        "url": "https://video.twimg.com/amplify_video/111/vid/avc1/480x270/1.mp4",
                    },
                    {
                        "bitrate": 2176000,
                        "content_type": "video/mp4",
                        "url": "https://video.twimg.com/amplify_video/111/vid/avc1/1280x720/2.mp4",
                    },
                    {
                        "bitrate": 832000,
                        "content_type": "video/mp4",
                        "url": "https://video.twimg.com/amplify_video/111/vid/avc1/640x360/3.mp4",
                    },
                    {
                        "bitrate": 100000,
                        "content_type": "video/mp4",
                        "url": "https://video.twimg.com/amplify_video/111/vid/avc1/1280x720/4.mp4",
                    },
                ],
            },
        },
        {
            "type": "video",
            "media_url_https": "https://pbs.twimg.com/ext_tw_video_thumb/222/pu/img/bbb.jpg",
            "video_info": {
                "duration_millis": 55300,
                "variants": [
                    {
                        "bitrate": 832000,
                        "content_type": "video/mp4",
                        "url": "https://video.twimg.com/ext_tw_video/222/pu/vid/avc1/640x360/5.mp4",
                    },
                    {
                        "bitrate": 2176000,
                        "content_type": "video/mp4",
                        "url": "https://video.twimg.com/ext_tw_video/222/pu/vid/avc1/1280x720/6.mp4?tag=12",
                    },
                ],
            },
        },
    ],
}

PHOTO_ONLY_SAMPLE = {
    "id_str": "999",
    "text": "photos only",
    "user": {"screen_name": "a"},
    "mediaDetails": [
        {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/x.jpg"}
    ],
}


# ---------------------------------------------------------------- URL parsing
class TestParseStatusUrls:
    def test_single_canonical(self):
        assert core.parse_status_urls("https://x.com/user/status/123456789") == [
            "https://x.com/user/status/123456789"
        ]

    def test_twitter_and_mobile_variants(self):
        for txt in (
            "https://twitter.com/user/status/123456789",
            "http://www.twitter.com/user/status/123456789",
            "mobile.twitter.com/user/status/123456789",
            "https://x.com/user/statuses/123456789",
        ):
            assert core.parse_status_urls(txt) == [
                "https://x.com/user/status/123456789"
            ]

    def test_no_scheme(self):
        assert core.parse_status_urls("x.com/user/status/123456789") == [
            "https://x.com/user/status/123456789"
        ]

    def test_suffixes_and_queries(self):
        assert core.parse_status_urls(
            "https://x.com/user/status/123456789/photo/1?s=20&t=abc#x"
        ) == ["https://x.com/user/status/123456789"]
        assert core.parse_status_urls(
            "https://x.com/user/status/123456789/video/1"
        ) == ["https://x.com/user/status/123456789"]

    def test_multiple_links_in_text(self):
        text = (
            "check this https://x.com/a/status/111 and this one too "
            "https://twitter.com/b/status/222?s=20 plus x.com/c/status/333"
        )
        assert core.parse_status_urls(text) == [
            "https://x.com/a/status/111",
            "https://x.com/b/status/222",
            "https://x.com/c/status/333",
        ]

    def test_dedupe_and_order(self):
        text = "x.com/a/status/111 x.com/a/status/111 x.com/b/status/222 x.com/a/status/111"
        assert core.parse_status_urls(text) == [
            "https://x.com/a/status/111",
            "https://x.com/b/status/222",
        ]

    def test_no_links(self):
        assert core.parse_status_urls("") == []
        assert core.parse_status_urls("hello world https://example.com/x") == []
        assert core.parse_status_urls("https://x.com/user") == []

    def test_profile_media_page_is_not_a_status(self):
        assert core.parse_status_urls("https://x.com/user/media") == []


# ---------------------------------------------------------------- parsing
class TestParseVideos:
    def test_two_videos(self):
        items = core.parse_videos(SYNDICATION_SAMPLE, "2122355344576037338")
        assert len(items) == 2
        assert all(i.total == 2 for i in items)
        assert [i.index for i in items] == [1, 2]
        assert items[0].uploader == "SpaceX"

    def test_formats_sorted_best_first_and_deduped(self):
        items = core.parse_videos(SYNDICATION_SAMPLE, "2122355344576037338")
        fmts = items[0].formats
        # HLS variant skipped, 720p duplicate resolved to the higher bitrate
        assert [f.height for f in fmts] == [720, 360, 270]
        assert fmts[0].url.endswith("2.mp4")
        assert all(f.content_type == "video/mp4" for f in fmts)

    def test_dimensions_from_url_with_query(self):
        items = core.parse_videos(SYNDICATION_SAMPLE, "2122355344576037338")
        best2 = items[1].best
        assert (best2.width, best2.height) == (1280, 720)

    def test_metadata(self):
        items = core.parse_videos(SYNDICATION_SAMPLE, "2122355344576037338")
        first = items[0]
        assert first.duration_ms == 19400
        assert first.duration_label == "0:19"
        assert first.thumbnail_url.endswith("aaa.jpg")
        assert first.quality_label == "720p"
        assert first.key == ("2122355344576037338", 1)
        assert first.title == "Starship 42 coming in for catch"

    def test_photo_only_post_has_no_videos(self):
        assert core.parse_videos(PHOTO_ONLY_SAMPLE, "999") == []

    def test_quote_tweet_falls_back_to_quoted_media(self):
        # the outer (quoting) tweet has no media of its own - search's
        # scroll-harvest can still pick it up because a video shows inside
        # its embedded quote card, so without this fallback this reports
        # zero videos despite one clearly being on the page
        data = {
            "id_str": "1000",
            "text": "look at this",
            "user": {"screen_name": "outerUser"},
            "mediaDetails": [],
            "quoted_tweet": {
                "id_str": "2000",
                "text": "original clip",
                "user": {"screen_name": "quotedUser"},
                "mediaDetails": [
                    {
                        "type": "video",
                        "media_url_https": "https://pbs.twimg.com/thumb.jpg",
                        "video_info": {
                            "duration_millis": 5000,
                            "variants": [
                                {
                                    "content_type": "video/mp4",
                                    "bitrate": 832000,
                                    "url": "https://video.twimg.com/ext_tw_video/2000/vid/720x1280/a.mp4",
                                },
                            ],
                        },
                    }
                ],
            },
        }
        items = core.parse_videos(data, "1000")
        assert len(items) == 1
        assert items[0].uploader == "quotedUser"
        assert items[0].webpage_url == "https://x.com/quotedUser/status/2000"
        assert items[0].formats[0].url.endswith("a.mp4")

    def test_quote_tweet_field_absent_stays_empty(self):
        data = {"id_str": "3000", "text": "plain", "user": {"screen_name": "u"}, "mediaDetails": []}
        assert core.parse_videos(data, "3000") == []


class TestFetchTweetTombstone:
    """Real-world capture: search's scroll-harvester can collect a tweet id
    whose video is only visible to a logged-in browser. The public
    syndication endpoint this app uses (no login) then returns a
    TweetTombstone placeholder instead of the tweet - no media to recover,
    ever, for that id. fetch_tweet() must say so clearly instead of letting
    it fall through to a generic "no videos found" message."""

    def test_tombstone_raises_clear_error(self):
        raw = json.dumps({"__typename": "TweetTombstone", "tombstone": {}}).encode()
        with mock.patch.object(core, "_http_get", return_value=raw):
            with pytest.raises(core.XGrabError, match="age-restricted"):
                core.fetch_tweet("123")

    def test_tombstone_is_specifically_a_tombstone_error(self):
        raw = json.dumps({"__typename": "TweetTombstone", "tombstone": {}}).encode()
        with mock.patch.object(core, "_http_get", return_value=raw):
            with pytest.raises(core.TombstoneError):
                core.fetch_tweet("123")

    def test_resolve_fallback_used_on_tombstone(self):
        raw = json.dumps({"__typename": "TweetTombstone", "tombstone": {}}).encode()
        fake_item = object()  # fetch_videos just needs a truthy list back

        def fallback(tweet_id):
            assert tweet_id == "123"
            return [fake_item]

        with mock.patch.object(core, "_http_get", return_value=raw):
            items = core.fetch_videos(
                "https://x.com/u/status/123", resolve_fallback=fallback
            )
        assert items == [fake_item]

    def test_resolve_fallback_empty_still_raises_tombstone(self):
        raw = json.dumps({"__typename": "TweetTombstone", "tombstone": {}}).encode()
        with mock.patch.object(core, "_http_get", return_value=raw):
            with pytest.raises(core.TombstoneError):
                core.fetch_videos(
                    "https://x.com/u/status/123", resolve_fallback=lambda tid: []
                )


# ---------------------------------------------------------------- selection
class TestPickFormat:
    def test_best(self):
        items = core.parse_videos(SYNDICATION_SAMPLE, "1")
        assert core.pick_format(items[0]).height == 720

    def test_cap(self):
        items = core.parse_videos(SYNDICATION_SAMPLE, "1")
        assert core.pick_format(items[0], 360).height == 360
        assert core.pick_format(items[0], 100).height == 270  # nothing lower -> lowest

    def test_no_formats_raises(self):
        item = core.VideoItem("1", 1, 1, "u", "t", None, "video", "x", [])
        with pytest.raises(core.XGrabError):
            core.pick_format(item)


# ---------------------------------------------------------------- helpers
class TestHelpers:
    def test_sanitize_filename(self):
        assert core.sanitize_filename('a<b>:c"/\\|?*d') == "abcd"
        assert core.sanitize_filename("   ") == "video"
        assert len(core.sanitize_filename("x" * 500)) <= 120

    def test_build_filename_multi(self):
        items = core.parse_videos(SYNDICATION_SAMPLE, "1")
        name = core.build_filename(items[0])
        assert name == "SpaceX-1-1-720p.mp4"

    def test_build_filename_single(self):
        item = core.VideoItem(
            "42", 1, 1, "Some:User", "text", 1000, "video", "x",
            [core.VideoFormat("video/mp4", 1280, 720, 1, "u")],
        )
        assert core.build_filename(item) == "SomeUser-42-720p.mp4"

    def test_format_duration(self):
        assert core.format_duration(None) == ""
        assert core.format_duration(19400) == "0:19"
        assert core.format_duration(72400) == "1:12"
        assert core.format_duration(3723400) == "1:02:03"

    def test_human_size(self):
        assert core.human_size(None) == ""
        assert core.human_size(500) == "500 B"
        assert core.human_size(1536) == "1.5 KB"
        assert core.human_size(5 * 1024 * 1024) == "5.0 MB"

    def test_thumbnail_url_for(self):
        items = core.parse_videos(SYNDICATION_SAMPLE, "1")
        url = core.thumbnail_url_for(items[1], "small")
        assert "name=small" in url and "format=jpg" in url

    def test_quality_options(self):
        items = core.parse_videos(SYNDICATION_SAMPLE, "1")
        assert core.quality_options(items) == ["720p", "360p", "270p"]

    def test_unique_path(self, tmp_path):
        p = str(tmp_path / "a.mp4")
        assert core.unique_path(p) == p
        open(p, "w").close()
        assert core.unique_path(p) == str(tmp_path / "a (1).mp4")
