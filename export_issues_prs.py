#!/usr/bin/env python3
"""Generate Markdown exports for GitHub issues and PRs from raw gh api JSON.

Expected raw layout (per repo slug "OWNER_REPO"):
  export/raw/OWNER_REPO/issues.json
  export/raw/OWNER_REPO/prs.json
  export/raw/OWNER_REPO/issue_comments/ISSUE-<number>.json
  export/raw/OWNER_REPO/pr_issue_comments/PR-<number>.json
  export/raw/OWNER_REPO/pr_review_comments/PR-<number>.json

Outputs (per repo slug under --out-root):
  <out-root>/OWNER_REPO/issues/ISSUE-<number>.md
  <out-root>/OWNER_REPO/prs/PR-<number>.md
  <out-root>/OWNER_REPO/assets/issues/<number>/...
  <out-root>/OWNER_REPO/assets/prs/<number>/...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import subprocess


IMG_PATTERN = re.compile(
    r"!\[[^\]]*\]\(\s*(?P<md_url>[^)\s]+)(?:\s+[\"\'][^\"\']*[\"\'])?\s*\)"
    r"|<img\b[^>]*?\bsrc=(?:(?P<html_q>[\"\'])(?P<html_url>.*?)(?P=html_q)|(?P<html_url_unq>[^>\s]+))[^>]*?>",
    re.IGNORECASE | re.DOTALL,
)

PR_CONTEXT_PATTERN = re.compile(
    r"(?i)(?:\bpr\b|\bpull\s+request\b|\bpull\b|\bmerge\b)\s*#(?P<num>\d+)"
)
PR_URL_PATTERN_TEMPLATE = r"https?://github\.com/{owner}/{repo}/pull/(?P<num>\d+)"

ISSUE_FIX_PATTERN = re.compile(
    r"(?i)\b(?:fixe[sd]?|close[sd]?|resolve[sd]?)\s+(?:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+))?#(?P<num>\d+)"
)


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def slugify_repo(repo: str) -> str:
    return repo.replace("/", "_")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sanitize_name(name: str) -> str:
    # ASCII-only safe filenames
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    cleaned = cleaned.strip("_")
    return cleaned or "image"


def filename_from_url(url: str, index: int) -> str:
    parsed = urlsplit(url)
    base = os.path.basename(parsed.path)
    if not base:
        base = "image"
    name, ext = os.path.splitext(base)
    name = sanitize_name(name)[:40]
    ext = ext.lower()
    if not ext or len(ext) > 10:
        ext = ".img"
    return f"{index:03d}_{name}{ext}"


def detect_ext_from_file(path: Path) -> Optional[str]:
    try:
        data = path.read_bytes()[:12]
    except Exception:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def get_auth_token() -> Optional[str]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip()
    # Try gh auth token if available
    try:
        import subprocess

        result = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
        )
        token = result.stdout.strip()
        return token or None
    except Exception:
        return None


def _candidate_urls(url: str) -> List[str]:
    # Some GitHub attachment URLs require a download hint to avoid 400
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "github.com" and parsed.path.startswith("/user-attachments/assets/"):
        if "download=1" not in parsed.query:
            return [url, url + ("&" if parsed.query else "?") + "download=1"]
    return [url]

def _is_user_attachment(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.netloc.lower() == "github.com" and parsed.path.startswith("/user-attachments/assets/")


def _download_with_gh(url: str, path: Path) -> bool:
    try:
        ensure_dir(path.parent)
        parsed = urlsplit(url)
        endpoint = parsed.path
        if parsed.query:
            endpoint = f"{endpoint}?{parsed.query}"
        # For user-attachments, force download=1 to get raw bytes
        if endpoint.startswith("/user-attachments/assets/") and "download=1" not in endpoint:
            endpoint = endpoint + ("&" if "?" in endpoint else "?") + "download=1"
        result = subprocess.run(
            [
                "gh",
                "api",
                "-X",
                "GET",
                "--hostname",
                "github.com",
                "-H",
                "Accept: application/octet-stream",
                endpoint,
                "-o",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if path.exists() and path.stat().st_size > 0:
            return True
    except Exception:
        return False
    return False


def download_image(url: str, path: Path, token: Optional[str]) -> bool:
    if path.exists():
        return True
    headers = {
        "User-Agent": "gh-issue-export/1.0",
        "Accept": "application/octet-stream",
    }
    if token:
        # Use token auth (works for api.github.com and usually for github.com)
        headers["Authorization"] = f"token {token}"
    last_exc: Optional[Exception] = None
    for candidate in _candidate_urls(url):
        try:
            req = Request(candidate, headers=headers)
            with urlopen(req) as resp:
                ensure_dir(path.parent)
                with path.open("wb") as f:
                    chunk = resp.read(1024 * 1024)
                    while chunk:
                        f.write(chunk)
                        chunk = resp.read(1024 * 1024)
            return True
        except Exception as exc:
            last_exc = exc
            continue
    # Fallback for github.com/user-attachments assets using gh client (auth cookies/token)
    parsed = urlsplit(url)
    if _is_user_attachment(url):
        if _download_with_gh(url, path):
            return True
    if not _is_user_attachment(url):
        eprint(f"WARN: Failed to download {url}: {last_exc}")
    return False


class ImageStats:
    def __init__(self) -> None:
        self.attempted = 0
        self.downloaded = 0
        self.failed = 0
        self.missing: List[Dict[str, str]] = []


class ImageTracker:
    def __init__(
        self,
        assets_dir: Path,
        md_dir: Path,
        token: Optional[str],
        stats: ImageStats,
        missing_cb,
    ) -> None:
        self.assets_dir = assets_dir
        self.md_dir = md_dir
        self.token = token
        self.stats = stats
        self.missing_cb = missing_cb
        self.counter = 0
        self.url_to_rel: Dict[str, str] = {}

    def get_local(self, url: str) -> str:
        if url in self.url_to_rel:
            return self.url_to_rel[url]
        self.counter += 1
        self.stats.attempted += 1
        filename = filename_from_url(url, self.counter)
        abs_path = self.assets_dir / filename
        ok = download_image(url, abs_path, self.token)
        if not ok:
            self.stats.failed += 1
            rel = os.path.relpath(abs_path, self.md_dir)
            rel = rel.replace(os.sep, "/")
            self.url_to_rel[url] = rel
            if self.missing_cb:
                self.missing_cb(url, rel)
            return rel
        # If extension is .img, try to detect real type and rename
        if abs_path.suffix.lower() == ".img":
            real_ext = detect_ext_from_file(abs_path)
            if real_ext:
                new_path = abs_path.with_suffix(real_ext)
                try:
                    abs_path.rename(new_path)
                    abs_path = new_path
                except Exception:
                    # If rename fails, keep .img
                    pass
        self.stats.downloaded += 1
        rel = os.path.relpath(abs_path, self.md_dir)
        rel = rel.replace(os.sep, "/")
        self.url_to_rel[url] = rel
        return rel


def replace_images(text: str, tracker: ImageTracker) -> str:
    if not text:
        return ""

    def repl(match: re.Match) -> str:
        url = match.group("md_url") or match.group("html_url") or match.group("html_url_unq")
        if not url:
            return match.group(0)
        if url.startswith("data:"):
            return match.group(0)
        if not url.startswith("http://") and not url.startswith("https://"):
            return match.group(0)
        local = tracker.get_local(url)
        return match.group(0).replace(url, local, 1)

    return IMG_PATTERN.sub(repl, text)


def parse_iso(dt: str) -> Tuple[str, float]:
    if not dt:
        return ("", float("inf"))
    # REST uses ISO 8601 with Z; string sort works, but we want numeric
    try:
        if dt.endswith("Z"):
            d = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        else:
            d = datetime.fromisoformat(dt)
        return (dt, d.timestamp())
    except Exception:
        return (dt, float("inf"))


def sort_comments(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    indexed = []
    for idx, c in enumerate(comments):
        _, ts = parse_iso(c.get("created_at") or c.get("createdAt") or "")
        indexed.append((ts, idx, c))
    indexed.sort(key=lambda x: (x[0], x[1]))
    return [c for _, _, c in indexed]


def extract_issue_fields(issue: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "number": issue.get("number"),
        "title": issue.get("title") or "",
        "url": issue.get("html_url") or issue.get("url") or "",
        "state": (issue.get("state") or "").upper(),
        "created_at": issue.get("created_at") or issue.get("createdAt") or "",
        "updated_at": issue.get("updated_at") or issue.get("updatedAt") or "",
        "body": issue.get("body") or "",
    }


def extract_pr_fields(pr: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "url": pr.get("html_url") or pr.get("url") or "",
        "state": (pr.get("state") or "").upper(),
        "created_at": pr.get("created_at") or pr.get("createdAt") or "",
        "updated_at": pr.get("updated_at") or pr.get("updatedAt") or "",
        "body": pr.get("body") or "",
    }


def load_comments(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except Exception as exc:
        eprint(f"WARN: Failed to read {path}: {exc}")
        return []
    if isinstance(data, list):
        return data
    # If gh api --paginate --slurp was used and returned list of pages
    if isinstance(data, dict):
        # Attempt to find list under common keys
        for key in ("comments", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def find_related_prs(texts: Iterable[str], pr_numbers: set, owner: str, repo: str) -> List[int]:
    related = set()
    url_pat = re.compile(PR_URL_PATTERN_TEMPLATE.format(owner=re.escape(owner), repo=re.escape(repo)), re.IGNORECASE)
    for text in texts:
        if not text:
            continue
        for m in url_pat.finditer(text):
            num = int(m.group("num"))
            if num in pr_numbers:
                related.add(num)
        for m in PR_CONTEXT_PATTERN.finditer(text):
            num = int(m.group("num"))
            if num in pr_numbers:
                related.add(num)
    return sorted(related)


def find_related_issues(pr_body: str, issue_numbers: set, owner: str, repo: str) -> List[int]:
    related = set()
    if not pr_body:
        return []
    for m in ISSUE_FIX_PATTERN.finditer(pr_body):
        num = int(m.group("num"))
        m_owner = m.group("owner")
        m_repo = m.group("repo")
        if m_owner and m_repo:
            if m_owner.lower() != owner.lower() or m_repo.lower() != repo.lower():
                continue
        if num in issue_numbers:
            related.add(num)
    return sorted(related)


def format_related_links(numbers: List[int], base_url: str, label: str) -> str:
    if not numbers:
        return "_None_"
    lines = []
    for num in numbers:
        url = f"{base_url}/{num}"
        lines.append(f"- [{label} #{num}]({url})")
    return "\n".join(lines)


def write_issue_md(
    issue: Dict[str, Any],
    comments: List[Dict[str, Any]],
    out_dir: Path,
    assets_root: Path,
    related_prs: List[int],
    repo_url: str,
    token: Optional[str],
    stats: ImageStats,
    missing_cb,
) -> None:
    num = issue["number"]
    md_path = out_dir / f"ISSUE-{num}.md"
    assets_dir = assets_root / str(num)
    tracker = ImageTracker(assets_dir, md_path.parent, token, stats, missing_cb)

    desc = replace_images(issue["body"], tracker)
    if not desc.strip():
        desc = "_No description_"

    comments_sorted = sort_comments(comments)

    comment_blocks = []
    for c in comments_sorted:
        author = (c.get("user", {}) or {}).get("login") or c.get("author", {}).get("login") or c.get("author") or "unknown"
        created = c.get("created_at") or c.get("createdAt") or ""
        body = c.get("body") or ""
        body = replace_images(body, tracker)
        if not body.strip():
            body = "_No content_"
        comment_blocks.append(f"### {author} | {created}\n\n{body}")

    related_section = format_related_links(related_prs, f"{repo_url}/pull", "PR")

    content = [
        f"# Issue #{num}: {issue['title']}",
        "",
        "- URL: " + issue["url"],
        "- State: " + issue["state"],
        "- Created: " + issue["created_at"],
        "- Updated: " + issue["updated_at"],
        "",
        "## Description",
        "",
        desc,
        "",
        "## Related PRs",
        "",
        related_section,
        "",
        "## Comments",
        "",
        "\n\n".join(comment_blocks) if comment_blocks else "_No comments_",
        "",
    ]

    ensure_dir(md_path.parent)
    md_path.write_text("\n".join(content), encoding="utf-8")


def write_pr_md(
    pr: Dict[str, Any],
    issue_comments: List[Dict[str, Any]],
    review_comments: List[Dict[str, Any]],
    out_dir: Path,
    assets_root: Path,
    related_issues: List[int],
    repo_url: str,
    token: Optional[str],
    stats: ImageStats,
    missing_cb,
) -> None:
    num = pr["number"]
    md_path = out_dir / f"PR-{num}.md"
    assets_dir = assets_root / str(num)
    tracker = ImageTracker(assets_dir, md_path.parent, token, stats, missing_cb)

    desc = replace_images(pr["body"], tracker)
    if not desc.strip():
        desc = "_No description_"

    combined_comments = []
    combined_comments.extend(issue_comments or [])
    combined_comments.extend(review_comments or [])
    combined_sorted = sort_comments(combined_comments)

    comment_blocks = []
    for c in combined_sorted:
        author = (c.get("user", {}) or {}).get("login") or c.get("author", {}).get("login") or c.get("author") or "unknown"
        created = c.get("created_at") or c.get("createdAt") or ""
        body = c.get("body") or ""
        body = replace_images(body, tracker)
        if not body.strip():
            body = "_No content_"
        comment_blocks.append(f"### {author} | {created}\n\n{body}")

    related_section = format_related_links(related_issues, f"{repo_url}/issues", "Issue")

    content = [
        f"# PR #{num}: {pr['title']}",
        "",
        "- URL: " + pr["url"],
        "- State: " + pr["state"],
        "- Created: " + pr["created_at"],
        "- Updated: " + pr["updated_at"],
        "",
        "## Description",
        "",
        desc,
        "",
        "## Related Issues",
        "",
        related_section,
        "",
        "## Comments",
        "",
        "\n\n".join(comment_blocks) if comment_blocks else "_No comments_",
        "",
    ]

    ensure_dir(md_path.parent)
    md_path.write_text("\n".join(content), encoding="utf-8")


def process_repo(repo: str, raw_root: Path, out_root: Path, token: Optional[str]) -> None:
    owner, name = repo.split("/", 1)
    slug = slugify_repo(repo)
    raw_dir = raw_root / slug

    issues_path = raw_dir / "issues.json"
    prs_path = raw_dir / "prs.json"
    if not issues_path.exists() or not prs_path.exists():
        eprint(f"ERROR: Missing raw files for {repo}. Expected {issues_path} and {prs_path}.")
        return

    issues_raw = load_json(issues_path)
    prs_raw = load_json(prs_path)

    if not isinstance(issues_raw, list):
        eprint(f"ERROR: {issues_path} is not a list. Make sure you used gh api --paginate.")
        return
    if not isinstance(prs_raw, list):
        eprint(f"ERROR: {prs_path} is not a list. Make sure you used gh api --paginate.")
        return

    issues = [extract_issue_fields(i) for i in issues_raw if "pull_request" not in i]
    prs = [extract_pr_fields(p) for p in prs_raw]

    issue_numbers = {i["number"] for i in issues if i.get("number") is not None}
    pr_numbers = {p["number"] for p in prs if p.get("number") is not None}

    repo_url = f"https://github.com/{owner}/{name}"

    out_repo = out_root / slug
    issues_dir = out_repo / "issues"
    prs_dir = out_repo / "prs"
    assets_issues = out_repo / "assets" / "issues"
    assets_prs = out_repo / "assets" / "prs"

    issue_comments_dir = raw_dir / "issue_comments"
    pr_issue_comments_dir = raw_dir / "pr_issue_comments"
    pr_review_comments_dir = raw_dir / "pr_review_comments"

    total_issues = len(issues)
    stats = ImageStats()
    missing: List[Dict[str, str]] = []
    total_items = len(issues) + len(prs)
    processed = 0
    last_percent = -1

    def maybe_log_progress() -> None:
        nonlocal last_percent, processed
        if total_items == 0:
            return
        percent = int((processed / total_items) * 100)
        # Log at 5% increments
        if percent // 5 != last_percent // 5:
            print(f"[{repo}] Progress: {percent}% ({processed}/{total_items})", flush=True)
            last_percent = percent

    def make_missing_cb(kind: str, number: int):
        def _cb(url: str, rel_path: str) -> None:
            missing.append(
                {
                    "repo": repo,
                    "repo_slug": slugify_repo(repo),
                    "kind": kind,
                    "number": str(number),
                    "url": url,
                    "local_path": rel_path.replace("../", ""),
                    "md_path": f"{kind}s/{kind.upper()}-{number}.md",
                }
            )
        return _cb

    for issue in issues:
        num = issue["number"]
        comments_path = issue_comments_dir / f"ISSUE-{num}.json"
        comments = load_comments(comments_path)
        related_prs = find_related_prs(
            [issue.get("body") or ""] + [c.get("body") or "" for c in comments],
            pr_numbers,
            owner,
            name,
        )
        write_issue_md(
            issue,
            comments,
            issues_dir,
            assets_issues,
            related_prs,
            repo_url,
            token,
            stats,
            make_missing_cb("issue", num),
        )
        processed += 1
        maybe_log_progress()

    for pr in prs:
        num = pr["number"]
        issue_comments_path = pr_issue_comments_dir / f"PR-{num}.json"
        review_comments_path = pr_review_comments_dir / f"PR-{num}.json"
        issue_comments = load_comments(issue_comments_path)
        review_comments = load_comments(review_comments_path)
        related_issues = find_related_issues(pr.get("body") or "", issue_numbers, owner, name)
        write_pr_md(
            pr,
            issue_comments,
            review_comments,
            prs_dir,
            assets_prs,
            related_issues,
            repo_url,
            token,
            stats,
            make_missing_cb("pr", num),
        )
        processed += 1
        maybe_log_progress()

    print(
        f"[{repo}] Images: downloaded {stats.downloaded}/{stats.attempted} "
        f"({stats.failed} failed)",
        flush=True,
    )
    if missing:
        missing_path = out_root / f"missing_attachments_{slugify_repo(repo)}.jsonl"
        with missing_path.open("w", encoding="utf-8") as f:
            for row in missing:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{repo}] Missing attachments list: {missing_path}", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export GitHub Issues/PRs to Markdown")
    p.add_argument(
        "--repo",
        action="append",
        required=True,
        help="Repo in the form OWNER/REPO (repeatable)",
    )
    p.add_argument(
        "--raw-root",
        default="export/raw",
        help="Root folder containing raw JSON (default: export/raw)",
    )
    p.add_argument(
        "--out-root",
        default="export",
        help="Root folder for Markdown output (default: export)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    token = get_auth_token()
    raw_root = Path(args.raw_root)
    out_root = Path(args.out_root)

    for repo in args.repo:
        if "/" not in repo:
            eprint(f"ERROR: Invalid repo format: {repo}")
            return 1
        process_repo(repo, raw_root, out_root, token)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
