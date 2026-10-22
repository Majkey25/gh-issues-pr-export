#!/usr/bin/env python3
"""Download GitHub issue/PR attachment URLs via a real browser session.

Reads JSONL files created by export_issues_prs.py:
  export/missing_attachments_<repo>.jsonl

Requires Playwright with a persistent profile (user logs in once).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

from export_issues_prs import MAX_DOWNLOADS, DownloadBudget, download_image, export_path

MAX_MANIFEST_LINE_CHARS = 64 * 1024


@dataclass(frozen=True)
class MissingAttachment:
    manifest: Path
    data: dict[str, str]
    line: str


def manifest_lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8") as stream:
        while line := stream.readline(MAX_MANIFEST_LINE_CHARS + 1):
            if len(line) > MAX_MANIFEST_LINE_CHARS:
                raise ValueError("Attachment manifest line is too long")
            yield line


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download GitHub attachments via browser session")
    p.add_argument("--out-root", default="export", help="Export root (default: export)")
    p.add_argument("--profile-dir", default="export/browser_profile", help="Browser profile dir")
    return p.parse_args()


def load_missing(out_root: Path) -> list[MissingAttachment]:
    files = sorted(out_root.glob("missing_attachments_*.jsonl"))
    rows: list[MissingAttachment] = []
    for f in files:
        manifest = export_path(out_root, f.name)
        for line in manifest_lines(manifest):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or any(
                not isinstance(row.get(key), str) for key in ("url", "repo_slug", "local_path")
            ) or any(not isinstance(value, str) for value in row.values()):
                raise ValueError("Invalid attachment manifest row")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+_[A-Za-z0-9_.-]+", row["repo_slug"]):
                raise ValueError("Invalid attachment repository slug")
            if not re.fullmatch(r"assets/(?:issues|prs)/[1-9][0-9]*/[0-9]{3,}_[A-Za-z0-9._-]+", row["local_path"]):
                raise ValueError("Invalid attachment asset path")
            export_path(out_root, f"{row['repo_slug']}/{row['local_path']}")
            rows.append(MissingAttachment(manifest, row, line))
            if len(rows) == MAX_DOWNLOADS:
                return rows
    return rows


def save_remaining(out_root: Path, completed: dict[Path, set[str]]) -> int:
    remaining = 0
    for path in sorted(out_root.glob("missing_attachments_*.jsonl")):
        manifest = export_path(out_root, path.name)
        successful = completed.get(manifest, set())
        if not successful:
            remaining += sum(bool(line.strip()) for line in manifest_lines(manifest))
            continue
        descriptor, name = tempfile.mkstemp(dir=out_root, suffix=".part")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                for line in manifest_lines(manifest):
                    if line not in successful:
                        output.write(line)
                        remaining += bool(line.strip())
            os.replace(temporary, manifest)
        finally:
            temporary.unlink(missing_ok=True)
    return remaining


def main() -> int:
    args = parse_args()
    out_root = Path(args.out_root)
    rows = load_missing(out_root)
    if not rows:
        print("No missing attachments found.")
        return 0

    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
        )
        page = context.new_page()
        page.goto("https://github.com")
        print("If not logged in, log in now in the opened browser.")
        input("Press Enter to continue downloading...")

        budget = DownloadBudget()
        ok = 0
        fail = 0
        completed: dict[Path, set[str]] = {}
        for attachment in rows:
            row = attachment.data
            url = row["url"]
            repo_slug = row["repo_slug"]
            rel = row["local_path"]
            target = export_path(out_root, f"{repo_slug}/{rel}")
            try:
                # Playwright filters by domain, path, and Secure before cookies leave the profile.
                cookie = "; ".join(
                    f"{c['name']}={c['value']}" for c in context.cookies(url) if "name" in c and "value" in c
                )
                if not download_image(url, target, None, budget, cookie=cookie):
                    fail += 1
                    continue
                ok += 1
                completed.setdefault(attachment.manifest, set()).add(attachment.line)
            except Exception:
                fail += 1
                continue

        remaining = save_remaining(out_root, completed)
        print(f"Downloaded {ok} attachments, {fail} failed; {remaining} remain for a later run.")
        context.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
