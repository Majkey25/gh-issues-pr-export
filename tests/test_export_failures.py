from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from export_issues_prs import main


class ExportFailureTest(unittest.TestCase):
    def test_invalid_raw_inputs_fail_and_valid_empty_export_succeeds(self) -> None:
        for issues, prs, expected in (
            (None, None, 1), ("{}", "[]", 1), ("[]", "{}", 1),
            ("{invalid", "[]", 1), ("[]", "[]", 0),
        ):
            with self.subTest(issues=issues, prs=prs), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw = root / "fixture_repo"
                raw.mkdir()
                if issues is not None:
                    (raw / "issues.json").write_text(issues, encoding="utf-8")
                if prs is not None:
                    (raw / "prs.json").write_text(prs, encoding="utf-8")
                argv = ["export", "--repo", "fixture/repo", "--raw-root", directory,
                        "--out-root", str(root / "out")]
                with patch("sys.argv", argv), patch("export_issues_prs.get_auth_token", return_value=None):
                    with redirect_stderr(StringIO()):
                        self.assertEqual(expected, main())


if __name__ == "__main__":
    unittest.main()
