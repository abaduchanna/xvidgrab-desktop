"""Offline tests for the hardened downloader (local loopback server only).

Simulates exactly the failure users saw on X's CDN:

    Unexpected error: IncompleteRead(0 bytes read, 46 more expected)

The local server declares a Content-Length but closes the connection early
(-> http.client.IncompleteRead on the client).  download_file() must retry
automatically and RESUME from the .part file via a Range request instead of
surfacing the raw exception or restarting from zero.
"""

import http.server
import os
import socketserver
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xgrab_core as core  # noqa: E402

FULL_BODY = bytes(range(256)) * 200  # 51_200 bytes of known bytes
FIRST_GOOD = 10_000                  # bytes delivered before the drop


class _TrickyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence test output
        pass

    def do_HEAD(self):  # used by the 416-completion path
        self.send_response(200)
        self.send_header("Content-Length", str(len(FULL_BODY)))
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng:
            # Resume request: serve the remainder as 206.
            offset = int(rng.split("=")[1].split("-")[0])
            remainder = FULL_BODY[offset:]
            self.send_response(206)
            self.send_header(
                "Content-Range",
                f"bytes {offset}-{len(FULL_BODY) - 1}/{len(FULL_BODY)}",
            )
            self.send_header("Content-Length", str(len(remainder)))
            self.end_headers()
            self.wfile.write(remainder)
            return
        # First attempt: declare the full length, send only part, then cut
        # the connection -> IncompleteRead on the client side.
        self.send_response(200)
        self.send_header("Content-Length", str(len(FULL_BODY)))
        self.end_headers()
        self.wfile.write(FULL_BODY[:FIRST_GOOD])
        self.wfile.flush()
        # Close the socket abruptly, mid-body.
        try:
            self.connection.shutdown(2)
        except OSError:
            pass
        self.connection.close()
        # Mark the handler thread as having dropped the connection.
        self.server.dropped = getattr(self.server, "dropped", 0) + 1


class DownloadHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _TrickyHandler)
        cls.srv.dropped = 0
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_incompletere_read_retries_and_resumes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "video.mp4")
            url = f"http://127.0.0.1:{self.port}/video.mp4"
            result = core.download_file(url, dest)
            self.assertEqual(result, dest)
            with open(dest, "rb") as fh:
                got = fh.read()
            self.assertEqual(got, FULL_BODY, "resumed download must be complete")
            # the server really did drop the connection on us at least once
            self.assertGreaterEqual(self.srv.dropped, 1)
            # no .part leftovers after success
            self.assertFalse(os.path.exists(dest + ".part"))
            self.assertFalse(os.path.exists(dest + ".part.meta"))

    def test_cancel_raises_xgraberror(self):
        import tempfile

        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "video.mp4")
            url = f"http://127.0.0.1:{self.port}/video.mp4"
            with self.assertRaises(core.XGrabError) as ctx:
                core.download_file(url, dest, cancel_event=cancel)
            self.assertEqual(str(ctx.exception), "Cancelled")

    def test_http_error_is_friendly(self):
        import tempfile

        class _404(_TrickyHandler):
            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv404 = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _404)
        t404 = threading.Thread(target=srv404.serve_forever, daemon=True)
        t404.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dest = os.path.join(tmp, "video.mp4")
                url = f"http://127.0.0.1:{srv404.server_address[1]}/video.mp4"
                with self.assertRaises(core.XGrabError) as ctx:
                    core.download_file(url, dest)
                self.assertIn("HTTP 404", str(ctx.exception))
        finally:
            srv404.shutdown()
            srv404.server_close()


if __name__ == "__main__":
    unittest.main()
