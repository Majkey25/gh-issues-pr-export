# gh-issues-pr-export

Export GitHub Issues and Pull Requests (including comments and images) to clean, deterministic Markdown. Built for private repos and repeatable runs.

## Features

- Exports all issues and PRs (open + closed)
- Includes bodies, comments, and review comments
- Downloads embedded images and rewrites links to local assets
- Preserves comment order
- Stores raw JSON for offline regeneration

## Requirements

- GitHub CLI (`gh`) installed and authenticated
- `uv` installed (creates the venv)
- Python 3 (provided by `uv` venv)

## Quick Start (Windows PowerShell)

1. Create and edit `.env`
```powershell
Copy-Item .env.example .env
notepad .env
```

2. Export issues + PRs
```powershell
cd C:\path\to\gh-issues-pr-export
powershell -ExecutionPolicy Bypass -File .\run_export.ps1
```

3. Download attachments and clean up images
```powershell
powershell -ExecutionPolicy Bypass -File .\run_download_attachments.ps1
powershell -ExecutionPolicy Bypass -File .\run_cleanup.ps1
```

## Quick Start (bash)

```bash
cp .env.example .env
${EDITOR:-nano} .env
./run_export.sh
```

## Configuration

Set values in `.env`:

```bash
EXPORT_REPOS=OWNER/REPO,OWNER/REPO
EXPORT_OUT_ROOT=export
EXPORT_RAW_ROOT=export/raw
EXPORT_PROFILE_DIR=export/browser_profile
# GH_TOKEN=
```

Notes:
- `EXPORT_REPOS` is required (comma-separated).
- `GH_TOKEN` is optional. Prefer `gh auth login`.
- `.env` is ignored by `.gitignore`.

## Output Structure

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

## Attachments (GitHub `user-attachments`)

Attachments hosted at `github.com/user-attachments/...` cannot be downloaded via API/PAT.
Use:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_download_attachments.ps1
```

Missing attachment URLs are tracked in:
```
export/missing_attachments_<repo>.jsonl
```

## Security

- Do not commit `export/`, `.env`, or any token.
- Use `gh auth login` instead of hardcoding credentials.
- If exports were ever committed, remove them from Git history.

## Troubleshooting

- `gh: Invalid request` → Re-run (scripts already use `-X GET`).
- `ERROR: missing command: uv` → install `uv`.
- `ERROR: missing command: python` → rerun after `uv` setup.
- Missing images → run `run_download_attachments.ps1`.

## License

MIT License. See `LICENSE`.
