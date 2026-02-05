#!/usr/bin/env python3
"""Download GitHub issue/PR attachment URLs via a real browser session.

Reads JSONL files created by export_issues_prs.py:
  export/missing_attachments_<repo>.jsonl

Requires Playwright with a persistent profile (user logs in once).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

from playwright.sync_api import sync_playwright


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download GitHub attachments via browser session")
    p.add_argument("--out-root", default="export", help="Export root (default: export)")
    p.add_argument("--profile-dir", default="export/browser_profile", help="Browser profile dir")
    return p.parse_args()


def load_missing(out_root: Path) -> List[Dict[str, str]]:
    files = sorted(out_root.glob("missing_attachments_*.jsonl"))
    rows: List[Dict[str, str]] = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


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

        req = context.request
        ok = 0
        fail = 0
        for row in rows:
            url = row["url"]
            repo_slug = row["repo_slug"]
            rel = row["local_path"]
            target = out_root / repo_slug / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                resp = req.get(url)
                if not resp.ok:
                    fail += 1
                    continue
                body = resp.body()
                if not body:
                    fail += 1
                    continue
                target.write_bytes(body)
                ok += 1
            except Exception:
                fail += 1
                continue

        print(f"Downloaded {ok} attachments, {fail} failed.")
        context.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
