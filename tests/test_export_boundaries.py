from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from download_attachments import load_missing, save_remaining
from export_issues_prs import export_path, process_repo


class ExportBoundaryTest(unittest.TestCase):
    def test_invalid_issue_and_pr_numbers_fail_before_output(self) -> None:
        for number in ("../../escape", "1", True, 0, -1, 2**53, None):
            for kind in ("issues", "prs"):
                with self.subTest(number=number, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    raw = root / "raw" / "fixture_repo"
                    raw.mkdir(parents=True)
                    for filename in ("issues", "prs"):
                        data = [{"number": number}] if kind == filename else []
                        (raw / f"{filename}.json").write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        process_repo("fixture/repo", root / "raw", root / "out", None)
                    self.assertFalse((root / "out").exists())

    def test_manifest_rejects_path_escapes_and_accepts_generated_rows(self) -> None:
        valid = {"url": "https://github.com/user-attachments/assets/id", "repo_slug": "fixture_repo",
                 "local_path": "assets/issues/1/001_image.png"}
        for key, value in (
            ("repo_slug", "../outside"), ("repo_slug", "C:\\outside"),
            ("local_path", "/outside"), ("local_path", "../outside"),
            ("local_path", "assets/issues/1/001_image.png:stream"),
            ("local_path", "assets\\issues\\1\\001_image.png"),
            ("local_path", "assets/issues/1/../../../outside"),
        ):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = root / "missing_attachments_fixture.jsonl"
                manifest.write_text(json.dumps(valid | {key: value}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_missing(root)
                manifest.write_text(json.dumps(valid), encoding="utf-8")
                self.assertEqual([valid], [attachment.data for attachment in load_missing(root)])

    def test_large_recovery_batch_preserves_failures_and_advances_across_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = []
            for repo, count in (("fixture_first", 600), ("fixture_second", 501)):
                manifest = root / f"missing_attachments_{repo}.jsonl"
                rows = [json.dumps({"url": f"https://example.com/{repo}/{number}", "repo_slug": repo,
                                    "local_path": f"assets/issues/{number}/001_image.png"}) + "\n"
                        for number in range(1, count + 1)]
                manifest.write_text("".join(rows), encoding="utf-8")
                expected.extend(rows)
            batch = load_missing(root)
            self.assertEqual(1000, len(batch))
            completed: dict[Path, set[str]] = {}
            for attachment in batch[1:]:
                completed.setdefault(attachment.manifest, set()).add(attachment.line)
            self.assertEqual(102, save_remaining(root, completed))
            retry = load_missing(root)
            self.assertEqual([expected[0], *expected[1000:]], [attachment.line for attachment in retry])
            completed = {}
            for attachment in retry:
                completed.setdefault(attachment.manifest, set()).add(attachment.line)
            self.assertEqual(0, save_remaining(root, completed))
            self.assertEqual([], load_missing(root))

    def test_manifest_replace_failure_keeps_pending_rows_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "missing_attachments_fixture.jsonl"
            original = '{"url":"https://example.com/image","repo_slug":"fixture_repo",' \
                       '"local_path":"assets/issues/1/001_image.png"}\n'
            manifest.write_text(original, encoding="utf-8")
            attachment = load_missing(root)[0]
            with patch("download_attachments.os.replace", side_effect=OSError("replace refused")):
                with self.assertRaises(OSError):
                    save_remaining(root, {manifest: {attachment.line}})
            self.assertEqual(original, manifest.read_text(encoding="utf-8"))
            self.assertEqual([manifest], list(root.iterdir()))

    def test_symlinked_destination_cannot_escape_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, outside = root / "out", root / "outside"
            output.mkdir()
            outside.mkdir()
            try:
                (output / "fixture_repo").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Host cannot create symlinks: {exc}")
            with self.assertRaises(ValueError):
                export_path(output, "fixture_repo/assets/issues/1/001_image.png")
            self.assertEqual([], list(outside.iterdir()))
            foreign = outside / "manifest.jsonl"
            foreign.write_text("private", encoding="utf-8")
            (output / "missing_attachments_fixture.jsonl").symlink_to(foreign)
            with self.assertRaises(ValueError):
                load_missing(output)
            with self.assertRaises(ValueError):
                save_remaining(output, {})
            self.assertEqual("private", foreign.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
