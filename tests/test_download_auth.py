from __future__ import annotations

import tempfile
import unittest
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import HTTPRedirectHandler, Request

from export_issues_prs import download_image


class DownloadAuthTest(unittest.TestCase):
    def test_interrupted_download_is_retried_without_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.png"
            response = MagicMock()
            response.__enter__.return_value.read.side_effect = [b"partial", OSError("interrupted")]
            with patch("export_issues_prs.urlopen", return_value=response):
                self.assertFalse(download_image("https://example.com/image.png", output, None))
            self.assertFalse(output.exists())
            self.assertEqual([], list(Path(directory).iterdir()))
            response.__enter__.return_value.read.side_effect = [b"complete", b""]
            with patch("export_issues_prs.urlopen", return_value=response):
                self.assertTrue(download_image("https://example.com/image.png", output, None))
            self.assertEqual(b"complete", output.read_bytes())

    def test_token_is_scoped_to_https_github_hosts_and_not_redirected(self) -> None:
        for url, authenticated in (
            ("https://github.com/image.png", True),
            ("https://api.github.com/image.png", True),
            ("https://example.com/image.png", False),
            ("https://github.com.example.com/image.png", False),
            ("http://github.com/image.png", False),
        ):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                response = MagicMock()
                response.__enter__.return_value.read.side_effect = [b"image", b""]
                with patch("export_issues_prs.urlopen", return_value=response) as open_url:
                    self.assertTrue(download_image(url, Path(directory) / "image.png", "dummy"))
                request: Request = open_url.call_args.args[0]
                self.assertEqual(
                    "token dummy" if authenticated else None,
                    request.get_header("Authorization"),
                )
                redirected = HTTPRedirectHandler().redirect_request(
                    request, BytesIO(), 302, "Found", HTTPMessage(), "https://example.com/redirect.png"
                )
                self.assertIsNotNone(redirected)
                assert redirected is not None
                self.assertIsNone(redirected.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
