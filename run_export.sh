#!/usr/bin/env bash
set -euo pipefail

# Load .env if present
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ".env"
  set +a
fi

# ============================
# CONFIG (set via environment)
# ============================
# Required: comma-separated list like "OWNER/REPO,OWNER/REPO"
: "${EXPORT_REPOS:?EXPORT_REPOS is not set. Example: EXPORT_REPOS=\"OWNER/REPO,OWNER/REPO\"}"

IFS=',' read -r -a REPOS <<< "$EXPORT_REPOS"
clean_repos=()
for repo in "${REPOS[@]}"; do
  # trim leading/trailing whitespace
  repo="${repo#"${repo%%[![:space:]]*}"}"
  repo="${repo%"${repo##*[![:space:]]}"}"
  if [ -n "$repo" ]; then
    clean_repos+=("$repo")
  fi
done
REPOS=("${clean_repos[@]}")
if [ "${#REPOS[@]}" -eq 0 ]; then
  echo "ERROR: EXPORT_REPOS is empty after parsing." >&2
  exit 1
fi

# Optional: override export locations
RAW_ROOT="${EXPORT_RAW_ROOT:-export/raw}"
OUT_ROOT="${EXPORT_OUT_ROOT:-export}"

# Optional: set GH_TOKEN if needed (private repos). If empty, script tries `gh auth token`.
# Token guidance (GitHub):
# - Recommended: Personal Access Token (classic)
# - Required scopes: `repo` (covers private repos + issues + PRs + comments)
# - Create at: GitHub -> Settings -> Developer settings -> Personal access tokens -> Tokens (classic)
# - Do NOT paste the real token into this file. Keep it empty and use `gh auth login` instead.
: "${GH_TOKEN:=}"

# ============================
# DO NOT EDIT BELOW
# ============================

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: missing command: $1" >&2
    exit 1
  fi
}

need_cmd gh
need_cmd uv

# Ensure gh auth
if ! gh auth status -h github.com >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated. Run: gh auth login --hostname github.com --scopes repo" >&2
  exit 1
fi

if [ -z "$GH_TOKEN" ]; then
  GH_TOKEN="$(gh auth token || true)"
  export GH_TOKEN
fi

mkdir -p "$RAW_ROOT"

for repo in "${REPOS[@]}"; do
  if [[ "$repo" != */* ]]; then
    echo "ERROR: invalid repo format: $repo (expected OWNER/REPO)" >&2
    exit 1
  fi

  owner="${repo%%/*}"
  name="${repo##*/}"
  slug="${owner}_${name}"
  base="${RAW_ROOT}/${slug}"

  mkdir -p "$base/issue_comments" "$base/pr_issue_comments" "$base/pr_review_comments"

  echo "==> Fetching issues for $repo"
  gh api -X GET "repos/${owner}/${name}/issues" -f state=all -f per_page=100 --paginate > "$base/issues.json"

  echo "==> Fetching PRs for $repo"
  gh api -X GET "repos/${owner}/${name}/pulls" -f state=all -f per_page=100 --paginate > "$base/prs.json"

  echo "==> Fetching issue comments for $repo"
  OWNER="$owner" REPO="$name" BASE="$base" python3 - <<'PY'
import json, os, subprocess

owner = os.environ["OWNER"]
repo = os.environ["REPO"]
base = os.environ["BASE"]

with open(f"{base}/issues.json", "r", encoding="utf-8") as f:
    issues = json.load(f)

for issue in issues:
    if "pull_request" in issue:
        continue
    num = issue["number"]
    out = f"{base}/issue_comments/ISSUE-{num}.json"
    with open(out, "w", encoding="utf-8") as w:
        subprocess.run(
            ["gh", "api", "-X", "GET", f"repos/{owner}/{repo}/issues/{num}/comments", "-f", "per_page=100", "--paginate"],
            check=True,
            stdout=w,
        )

with open(f"{base}/prs.json", "r", encoding="utf-8") as f:
    prs = json.load(f)

for pr in prs:
    num = pr["number"]
    out_issue = f"{base}/pr_issue_comments/PR-{num}.json"
    with open(out_issue, "w", encoding="utf-8") as w:
        subprocess.run(
            ["gh", "api", "-X", "GET", f"repos/{owner}/{repo}/issues/{num}/comments", "-f", "per_page=100", "--paginate"],
            check=True,
            stdout=w,
        )

    out_review = f"{base}/pr_review_comments/PR-{num}.json"
    with open(out_review, "w", encoding="utf-8") as w:
        subprocess.run(
            ["gh", "api", "-X", "GET", f"repos/{owner}/{repo}/pulls/{num}/comments", "-f", "per_page=100", "--paginate"],
            check=True,
            stdout=w,
        )
PY

done

# Create venv via uv (if not exists) and install requirements
if [ ! -d ".venv" ]; then
  uv venv .venv
fi

if [ -f "requirements.txt" ]; then
  uv pip install -r requirements.txt
fi

# Generate Markdown and download images
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
.venv/bin/python "$SCRIPT_DIR/export_issues_prs.py" \
  --raw-root "$RAW_ROOT" \
  --out-root "$OUT_ROOT" \
  $(printf -- "--repo %q " "${REPOS[@]}")

echo "Done. Output is in $OUT_ROOT/" 
