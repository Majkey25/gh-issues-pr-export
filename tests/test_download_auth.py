from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from export_issues_prs import DownloadBudget, ImageStats, ImageTracker, download_image, process_repo, public_connection


class DownloadAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[dict[str, str]] = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append(dict(self.headers.items()))
                if self.path in {"/redirect", "/redirect-public"}:
                    self.send_response(302)
                    target = "http://127.0.0.1/private" if self.path == "/redirect" else "http://example.com/ok"
                    self.send_header("Location", target)
                    self.end_headers()
                    return
                self.send_response(200)
                if self.path == "/partial":
                    self.send_header("Content-Length", "100")
                self.end_headers()
                if self.path == "/slow":
                    time.sleep(0.2)
                try:
                    if self.path.endswith("/image"):
                        self.wfile.write(b"\x89PNG\r\n\x1a\n")
                    self.wfile.write(b"updated" if self.path.startswith("/new/") else b"complete")
                except OSError:
                    pass

            def log_message(self, format: str, *args: str) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def local_connection(self, url: str) -> HTTPConnection:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=1)
        connection.connect()
        return connection

    def test_interrupted_download_is_retried_without_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "export_issues_prs.public_connection", side_effect=self.local_connection,
        ):
            output = Path(directory) / "image.png"
            self.assertFalse(download_image("http://example.com/partial", output, None))
            self.assertEqual([], list(Path(directory).iterdir()))
            self.assertTrue(download_image("http://example.com/ok", output, None))
            self.assertEqual(b"complete", output.read_bytes())

    def test_token_and_cookie_are_scoped_and_not_redirected(self) -> None:
        for url, authenticated, cookie in (
            ("https://github.com/redirect-public", True, True),
            ("https://api.github.com/redirect-public", True, False),
            ("https://example.com/redirect-public", False, False),
            ("https://github.com.example.com/redirect-public", False, False),
            ("http://github.com/redirect-public", False, False),
        ):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory, patch(
                "export_issues_prs.public_connection", side_effect=self.local_connection,
            ):
                self.requests.clear()
                self.assertTrue(download_image(url, Path(directory) / "image.png", "dummy", cookie="session=dummy"))
                self.assertEqual("token dummy" if authenticated else None, self.requests[0].get("Authorization"))
                self.assertEqual("session=dummy" if cookie else None, self.requests[0].get("Cookie"))
                self.assertNotIn("Authorization", self.requests[1])
                self.assertNotIn("Cookie", self.requests[1])

    def test_private_destinations_and_redirects_never_connect(self) -> None:
        for host in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "[::1]"):
            with self.subTest(host=host), patch("export_issues_prs.socket.socket") as connect:
                with self.assertRaises(ValueError):
                    public_connection(f"http://{host}/")
                connect.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            def first_public_only(url: str) -> HTTPConnection:
                return self.local_connection(url) if url == "http://example.com/redirect" else public_connection(url)

            with patch("export_issues_prs.public_connection", side_effect=first_public_only):
                self.assertFalse(download_image("http://example.com/redirect", Path(directory) / "image", None))
            self.assertEqual(1, len(self.requests))
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_reexport_refreshes_replaced_assets_and_removes_unreferenced_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "export_issues_prs.public_connection", side_effect=self.local_connection,
        ):
            root = Path(directory)
            raw = root / "raw" / "fixture_repo"
            raw.mkdir(parents=True)
            (raw / "prs.json").write_text("[]", encoding="utf-8")
            out = root / "out"
            asset = out / "fixture_repo" / "assets" / "issues" / "1" / "001_image.png"
            for body, expected in (
                ("![image](http://example.com/old/image)", b"\x89PNG\r\n\x1a\ncomplete"),
                ("![image](http://example.com/new/image)", b"\x89PNG\r\n\x1a\nupdated"),
                ("", None),
            ):
                (raw / "issues.json").write_text(json.dumps([{"number": 1, "body": body}]), encoding="utf-8")
                process_repo("fixture/repo", root / "raw", out, None)
                if expected is None:
                    self.assertFalse(asset.exists())
                else:
                    self.assertEqual(expected, asset.read_bytes())
            manifest = out / "missing_attachments_fixture_repo.jsonl"
            manifest.write_text("stale", encoding="utf-8")
            process_repo("fixture/repo", root / "raw", out, None)
            self.assertEqual("", manifest.read_text(encoding="utf-8"))

    def test_dns_address_is_pinned_and_tls_keeps_original_hostname(self) -> None:
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("export_issues_prs.socket.getaddrinfo", return_value=addresses) as lookup, patch(
            "export_issues_prs.socket.socket",
        ) as sock, patch("export_issues_prs.ssl.create_default_context") as context:
            connection = public_connection("https://example.com/image")
            lookup.assert_called_once()
            sock.return_value.connect.assert_called_once_with(("93.184.216.34", 443))
            context.return_value.wrap_socket.assert_called_once_with(sock.return_value, server_hostname="example.com")
            connection.close()

    def test_trailing_dot_image_name_uses_safe_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "export_issues_prs.public_connection", side_effect=self.local_connection,
        ):
            root = Path(directory)
            tracker = ImageTracker(root / "assets", root, None, ImageStats(), None)
            self.assertEqual("assets/001_photo.img", tracker.get_local("https://example.com/photo."))
            self.assertEqual(b"complete", (root / "assets" / "001_photo.img").read_bytes())

    def test_size_count_aggregate_and_slow_response_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "export_issues_prs.public_connection", side_effect=self.local_connection,
        ):
            output = Path(directory) / "image"
            with patch("export_issues_prs.MAX_IMAGE_BYTES", 3):
                self.assertFalse(download_image("http://example.com/ok", output, None))
            with patch("export_issues_prs.MAX_DOWNLOAD_BYTES", 10):
                budget = DownloadBudget()
                self.assertTrue(download_image("http://example.com/ok", output, None, budget))
                self.assertFalse(download_image("http://example.com/ok", output, None, budget))
                self.assertEqual(b"complete", output.read_bytes())
            with patch("export_issues_prs.MAX_DOWNLOADS", 1):
                budget = DownloadBudget()
                self.assertTrue(download_image("http://example.com/ok", output, None, budget))
                self.assertFalse(download_image("http://example.com/ok", output, None, budget))
            with patch("export_issues_prs.DOWNLOAD_TIMEOUT", 0.05):
                self.assertFalse(download_image("http://example.com/slow", output, None))
            self.assertEqual([output], list(Path(directory).iterdir()))


if __name__ == "__main__":
    unittest.main()
