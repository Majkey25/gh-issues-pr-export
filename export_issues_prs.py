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
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_DOWNLOADS = 1000
DOWNLOAD_TIMEOUT = 30
EXPORT_TIMEOUT = 15 * 60

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
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(
        part in {".", ".."} for part in repo.split("/")
    ):
        raise ValueError("Invalid repository name")
    return repo.replace("/", "_")


def export_path(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if any(
        not part or part in {".", ".."} or part.endswith((".", " "))
        or re.search(r'[\\:<>"|?*\x00-\x1f]', part)
        or re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
        for part in parts
    ):
        raise ValueError("Invalid export path")
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Export path escapes output root")
    return target


def item_number(value: Any) -> int:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise ValueError("Issue/PR number must be a positive integer")
    return value


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
    ext = sanitize_name(ext.lower()) if ext else ""
    if not ext or ext == "." or len(ext) > 10:
        ext = ".img"
    return f"{index:03d}_{name}{ext}"


def detect_ext_from_file(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(12)
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


def get_auth_token() -> str | None:
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


def _candidate_urls(url: str) -> list[str]:
    # Some GitHub attachment URLs require a download hint to avoid 400
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "github.com" and parsed.path.startswith("/user-attachments/assets/"):
        if "download=1" not in parsed.query:
            return [url, url + ("&" if parsed.query else "?") + "download=1"]
    return [url]


def _is_user_attachment(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.netloc.lower() == "github.com" and parsed.path.startswith("/user-attachments/assets/")


class DownloadBudget:
    def __init__(self) -> None:
        self.count = 0
        self.bytes = 0
        self.deadline = time.monotonic() + EXPORT_TIMEOUT


def public_connection(url: str) -> HTTPConnection:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) image URLs are allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError("Non-public image destination refused")
    if not addresses:
        raise OSError("Image destination has no addresses")
    # Connect to the checked address, so a second DNS lookup cannot change the destination.
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    last_error: OSError | None = None
    for family, kind, protocol, _, address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Image connection time limit exceeded")
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(remaining)
            sock.connect(address)
            connection = HTTPConnection(parsed.hostname, port, timeout=DOWNLOAD_TIMEOUT)
            if parsed.scheme == "https":
                context = ssl.create_default_context()
                context.set_alpn_protocols(["http/1.1"])
                sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
            connection.sock = sock
            return connection
        except OSError as exc:
            last_error = exc
            sock.close()
    raise OSError("Could not connect to image destination") from last_error


def download_image(
    url: str, path: Path, token: str | None, budget: DownloadBudget | None = None, *, cookie: str = "",
) -> bool:
    budget = budget if budget is not None else DownloadBudget()
    if budget.count >= MAX_DOWNLOADS or budget.bytes >= MAX_DOWNLOAD_BYTES or time.monotonic() >= budget.deadline:
        return False
    budget.count += 1
    ensure_dir(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, suffix=".part")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        last_exc: Exception | None = None
        for candidate in _candidate_urls(url):
            try:
                for redirects in range(6):
                    if time.monotonic() >= budget.deadline:
                        raise TimeoutError("Export download time limit exceeded")
                    parsed = urlsplit(candidate)
                    headers = {"User-Agent": "gh-issue-export/1.0", "Accept": "application/octet-stream"}
                    if redirects == 0 and parsed.scheme == "https":
                        if token and parsed.hostname in {"github.com", "api.github.com"}:
                            headers["Authorization"] = f"token {token}"
                        if cookie and parsed.hostname == "github.com":
                            headers["Cookie"] = cookie
                    connection = public_connection(candidate)
                    sock = connection.sock

                    def interrupt(sock: socket.socket | None = sock) -> None:
                        try:
                            if sock is not None:
                                sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

                    request_deadline = min(time.monotonic() + DOWNLOAD_TIMEOUT, budget.deadline)
                    timer = threading.Timer(max(0, request_deadline - time.monotonic()), interrupt)
                    timer.daemon = True
                    timer.start()
                    try:
                        endpoint = parsed.path or "/"
                        if parsed.query:
                            endpoint += f"?{parsed.query}"
                        connection.request("GET", endpoint, headers=headers)
                        with connection.getresponse() as resp:
                            if resp.status in {301, 302, 303, 307, 308}:
                                location = resp.getheader("Location")
                                if not location or redirects == 5:
                                    raise ValueError("Invalid or excessive image redirects")
                                candidate = urljoin(candidate, location)
                                continue
                            if resp.status != 200:
                                raise OSError(f"Image HTTP status {resp.status}")
                            length = resp.getheader("Content-Length")
                            remaining = min(MAX_IMAGE_BYTES, MAX_DOWNLOAD_BYTES - budget.bytes)
                            if length is not None and not 0 < int(length) <= remaining:
                                raise ValueError("Image response exceeds byte limit or is empty")
                            size = 0
                            with temporary.open("wb") as output:
                                while chunk := resp.read1(min(64 * 1024, remaining - size + 1)):
                                    size += len(chunk)
                                    budget.bytes += len(chunk)
                                    if size > remaining or time.monotonic() >= request_deadline:
                                        raise ValueError("Image download budget exceeded")
                                    output.write(chunk)
                            if time.monotonic() >= request_deadline:
                                raise TimeoutError("Image response time limit exceeded")
                            if size == 0 or (length is not None and size != int(length)):
                                raise OSError("Empty or incomplete image response")
                        temporary.replace(path)
                        return True
                    finally:
                        timer.cancel()
                        connection.close()
            except Exception as exc:
                last_exc = exc
                continue
        if not _is_user_attachment(url):
            eprint(f"WARN: Failed to download {url}: {last_exc}")
        return False
    finally:
        temporary.unlink(missing_ok=True)


class ImageStats:
    def __init__(self) -> None:
        self.attempted = 0
        self.downloaded = 0
        self.failed = 0
        self.missing: list[dict[str, str]] = []
        self.budget = DownloadBudget()


class ImageTracker:
    def __init__(
        self,
        assets_dir: Path,
        md_dir: Path,
        token: str | None,
        stats: ImageStats,
        missing_cb,
    ) -> None:
        self.assets_dir = assets_dir
        self.md_dir = md_dir
        self.token = token
        self.stats = stats
        self.missing_cb = missing_cb
        self.counter = 0
        self.url_to_rel: dict[str, str] = {}
        self.downloaded_paths: set[Path] = set()

    def get_local(self, url: str) -> str:
        if url in self.url_to_rel:
            return self.url_to_rel[url]
        self.counter += 1
        self.stats.attempted += 1
        filename = filename_from_url(url, self.counter)
        abs_path = export_path(self.assets_dir, filename)
        ok = download_image(url, abs_path, self.token, self.stats.budget)
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
                new_path = export_path(self.assets_dir, abs_path.with_suffix(real_ext).name)
                try:
                    abs_path.replace(new_path)
                    abs_path = new_path
                except Exception:
                    # If rename fails, keep .img
                    pass
        self.stats.downloaded += 1
        self.downloaded_paths.add(abs_path.resolve())
        rel = os.path.relpath(abs_path, self.md_dir)
        rel = rel.replace(os.sep, "/")
        self.url_to_rel[url] = rel
        return rel

    def remove_stale(self) -> None:
        if self.assets_dir.exists():
            for path in self.assets_dir.iterdir():
                if re.fullmatch(r"\d{3,}_[A-Za-z0-9._-]+", path.name) and path.resolve() not in self.downloaded_paths:
                    export_path(self.assets_dir, path.name).unlink()


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


def parse_iso(dt: str) -> tuple[str, float]:
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


def sort_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = []
    for idx, c in enumerate(comments):
        _, ts = parse_iso(c.get("created_at") or c.get("createdAt") or "")
        indexed.append((ts, idx, c))
    indexed.sort(key=lambda x: (x[0], x[1]))
    return [c for _, _, c in indexed]


def get_author_login(comment: Mapping[str, Any]) -> str:
    user = comment.get("user")
    if isinstance(user, Mapping):
        login = user.get("login")
        if isinstance(login, str) and login:
            return login

    author = comment.get("author")
    if isinstance(author, Mapping):
        login = author.get("login")
        if isinstance(login, str) and login:
            return login
    if isinstance(author, str) and author:
        return author
    return "unknown"


def extract_issue_fields(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": item_number(issue.get("number")),
        "title": issue.get("title") or "",
        "url": issue.get("html_url") or issue.get("url") or "",
        "state": (issue.get("state") or "").upper(),
        "created_at": issue.get("created_at") or issue.get("createdAt") or "",
        "updated_at": issue.get("updated_at") or issue.get("updatedAt") or "",
        "body": issue.get("body") or "",
    }


def extract_pr_fields(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": item_number(pr.get("number")),
        "title": pr.get("title") or "",
        "url": pr.get("html_url") or pr.get("url") or "",
        "state": (pr.get("state") or "").upper(),
        "created_at": pr.get("created_at") or pr.get("createdAt") or "",
        "updated_at": pr.get("updated_at") or pr.get("updatedAt") or "",
        "body": pr.get("body") or "",
    }


def load_comments(path: Path) -> list[dict[str, Any]]:
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


def find_related_prs(texts: Iterable[str], pr_numbers: set, owner: str, repo: str) -> list[int]:
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


def find_related_issues(pr_body: str, issue_numbers: set, owner: str, repo: str) -> list[int]:
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


def format_related_links(numbers: list[int], base_url: str, label: str) -> str:
    if not numbers:
        return "_None_"
    lines = []
    for num in numbers:
        url = f"{base_url}/{num}"
        lines.append(f"- [{label} #{num}]({url})")
    return "\n".join(lines)


def write_issue_md(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    out_dir: Path,
    assets_root: Path,
    related_prs: list[int],
    repo_url: str,
    token: str | None,
    stats: ImageStats,
    missing_cb,
) -> None:
    num = item_number(issue["number"])
    md_path = export_path(out_dir, f"ISSUE-{num}.md")
    assets_dir = export_path(assets_root, str(num))
    tracker = ImageTracker(assets_dir, md_path.parent, token, stats, missing_cb)

    desc = replace_images(issue["body"], tracker)
    if not desc.strip():
        desc = "_No description_"

    comments_sorted = sort_comments(comments)

    comment_blocks = []
    for c in comments_sorted:
        author = get_author_login(c)
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
    tracker.remove_stale()


def write_pr_md(
    pr: dict[str, Any],
    issue_comments: list[dict[str, Any]],
    review_comments: list[dict[str, Any]],
    out_dir: Path,
    assets_root: Path,
    related_issues: list[int],
    repo_url: str,
    token: str | None,
    stats: ImageStats,
    missing_cb,
) -> None:
    num = item_number(pr["number"])
    md_path = export_path(out_dir, f"PR-{num}.md")
    assets_dir = export_path(assets_root, str(num))
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
        author = get_author_login(c)
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
    tracker.remove_stale()


def process_repo(repo: str, raw_root: Path, out_root: Path, token: str | None) -> None:
    owner, name = repo.split("/", 1)
    slug = slugify_repo(repo)
    raw_dir = raw_root / slug

    issues_path = raw_dir / "issues.json"
    prs_path = raw_dir / "prs.json"
    if not issues_path.exists() or not prs_path.exists():
        raise FileNotFoundError(f"Missing raw files for {repo}. Expected {issues_path} and {prs_path}.")

    issues_raw = load_json(issues_path)
    prs_raw = load_json(prs_path)

    if not isinstance(issues_raw, list):
        raise ValueError(f"{issues_path} is not a list. Make sure you used gh api --paginate.")
    if not isinstance(prs_raw, list):
        raise ValueError(f"{prs_path} is not a list. Make sure you used gh api --paginate.")

    issues = [extract_issue_fields(i) for i in issues_raw if "pull_request" not in i]
    prs = [extract_pr_fields(p) for p in prs_raw]

    issue_numbers = {i["number"] for i in issues if i.get("number") is not None}
    pr_numbers = {p["number"] for p in prs if p.get("number") is not None}

    repo_url = f"https://github.com/{owner}/{name}"

    issues_dir = export_path(out_root, f"{slug}/issues")
    prs_dir = export_path(out_root, f"{slug}/prs")
    assets_issues = export_path(out_root, f"{slug}/assets/issues")
    assets_prs = export_path(out_root, f"{slug}/assets/prs")

    issue_comments_dir = raw_dir / "issue_comments"
    pr_issue_comments_dir = raw_dir / "pr_issue_comments"
    pr_review_comments_dir = raw_dir / "pr_review_comments"

    stats = ImageStats()
    missing: list[dict[str, str]] = []
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
    ensure_dir(out_root)
    missing_path = export_path(out_root, f"missing_attachments_{slug}.jsonl")
    with missing_path.open("w", encoding="utf-8") as f:
        for row in missing:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if missing:
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
        try:
            process_repo(repo, raw_root, out_root, token)
        except (OSError, ValueError) as exc:
            eprint(f"ERROR: {exc}")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
