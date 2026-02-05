# gh-issues-pr-export

Export GitHub Issues and Pull Requests (including comments and images) to clean, deterministic Markdown. Built for private repos and repeatable runs.

**Features**
- Exports all issues and PRs (open + closed)
- Includes bodies, comments, and review comments
- Downloads embedded images and rewrites links to local assets
- Preserves comment order
- Stores raw JSON for offline regeneration

**Requirements**
- Git
- GitHub CLI (`gh`) installed and authenticated
- `uv` installed (creates the venv)
- Python 3 (provided by `uv` venv)
- Internet access to GitHub

**Install (clone from GitHub)**
```bash
git clone https://github.com/OWNER/REPO.git
cd REPO
```

**Configuration (.env)**
Create `.env` in the repo root:
```bash
EXPORT_REPOS=OWNER/REPO,OWNER/REPO
EXPORT_OUT_ROOT=export
EXPORT_RAW_ROOT=export/raw
EXPORT_PROFILE_DIR=export/browser_profile
# GH_TOKEN=
```

Notes:
- `EXPORT_REPOS` is required (comma-separated `OWNER/REPO`).
- `GH_TOKEN` is optional. Prefer `gh auth login`.
- `.env` is ignored by `.gitignore`.

**Quick Start (Windows PowerShell)**

1. Create and edit `.env`
```powershell
Copy-Item .env.example .env
notepad .env
```

2. Download Markdown (issues + PRs)
```powershell
cd C:\path\to\gh-issues-pr-export
powershell -ExecutionPolicy Bypass -File .\run_export.ps1
```

3. Download images hosted on `github.com/user-attachments/...`
```powershell
powershell -ExecutionPolicy Bypass -File .\run_download_attachments.ps1
```

4. Rename/normalize image extensions
```powershell
powershell -ExecutionPolicy Bypass -File .\run_cleanup.ps1
```

**Quick Start (bash)**

1. Create and edit `.env`
```bash
cp .env.example .env
${EDITOR:-nano} .env
```

2. Download Markdown (issues + PRs)
```bash
./run_export.sh
```

3. Download images hosted on `github.com/user-attachments/...`
```bash
test -d .venv || uv venv .venv
uv pip install -r requirements-download.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python download_attachments.py --out-root export --profile-dir export/browser_profile
```

4. Rename/normalize image extensions
```bash
.venv/bin/python cleanup_img_ext.py --export-root export --debug
```

**Output Structure**
```
export/
  OWNER_REPO/
    issues/ISSUE-<number>.md
    prs/PR-<number>.md
    assets/
      issues/<number>/...
      prs/<number>/...
  OWNER_REPO_2/
    ...
  raw/
    OWNER_REPO/
      issues.json
      prs.json
      issue_comments/ISSUE-<number>.json
      pr_issue_comments/PR-<number>.json
      pr_review_comments/PR-<number>.json
    OWNER_REPO_2/
      ...
```

**Attachments (GitHub `user-attachments`)**
Attachments hosted at `github.com/user-attachments/...` cannot be downloaded via API/PAT.
Use Step 3 above. Missing attachment URLs are tracked in:
```
export/missing_attachments_<repo>.jsonl
```

**Security**
- Do not commit `export/`, `.env`, or any token.
- Use `gh auth login` instead of hardcoding credentials.
- If exports were ever committed, remove them from Git history.

**Troubleshooting**
- `gh: Invalid request` -> Re-run (scripts already use `-X GET`).
- `ERROR: missing command: uv` -> Install `uv`.
- `ERROR: missing command: python` -> Rerun after `uv` setup.
- Missing images -> Run Step 3 (attachments downloader).

**License**
MIT License. See `LICENSE`.
