$ErrorActionPreference = "Stop"
# Force UTF-8 so gh output isn't mojibake (Czech text).
$utf8 = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = $utf8
[Console]::OutputEncoding = $utf8

# ============================
# .env loader (optional)
# ============================
function Load-DotEnv {
  param([string]$Path)
  if (-not (Test-Path $Path)) { return }
  Get-Content $Path | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $parts = $line.Split("=", 2)
    if ($parts.Count -ne 2) { return }
    $key = $parts[0].Trim()
    $val = $parts[1].Trim()
    if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
      $val = $val.Substring(1, $val.Length - 2)
    }
    if ($key) { [Environment]::SetEnvironmentVariable($key, $val) }
  }
}

# Load .env from repo root (if present)
Load-DotEnv (Join-Path $PSScriptRoot ".env")

# ============================
# CONFIG (set via environment)
# ============================
# Required: comma-separated list like "OWNER/REPO,OWNER/REPO"
$ReposEnv = $Env:EXPORT_REPOS
if (-not $ReposEnv -or $ReposEnv.Trim() -eq "") {
  throw "ERROR: EXPORT_REPOS is not set. Example: `$Env:EXPORT_REPOS = `"OWNER/REPO,OWNER/REPO`""
}
$Repos = $ReposEnv.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }

# Optional: override export locations
$RawRoot = if ($Env:EXPORT_RAW_ROOT -and $Env:EXPORT_RAW_ROOT.Trim() -ne "") { $Env:EXPORT_RAW_ROOT } else { "export/raw" }
$OutRoot = if ($Env:EXPORT_OUT_ROOT -and $Env:EXPORT_OUT_ROOT.Trim() -ne "") { $Env:EXPORT_OUT_ROOT } else { "export" }

# Optional: set GH_TOKEN if needed (private repos). If empty, script tries `gh auth token`.
# Token guidance (GitHub):
# - Recommended: Personal Access Token (classic)
# - Required scopes: `repo` (covers private repos + issues + PRs + comments)
# - Create at: GitHub -> Settings -> Developer settings -> Personal access tokens -> Tokens (classic)
# - Do NOT paste the real token into this file. Use `gh auth login` instead.

# ============================
# DO NOT EDIT BELOW
# ============================

function Require-Command([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "ERROR: missing command: $Name"
  }
}

function Ensure-UvVenv {
  $uv = Get-Command uv -ErrorAction SilentlyContinue
  if (-not $uv) {
    throw "ERROR: missing command: uv (install from https://astral.sh/uv)"
  }

  $venvPath = Join-Path $PSScriptRoot ".venv"
  if (-not (Test-Path $venvPath)) {
    & uv venv $venvPath | Out-Null
  }

  $pyPath = Join-Path $venvPath "Scripts\\python.exe"
  if (-not (Test-Path $pyPath)) {
    throw "ERROR: uv venv created but python not found at $pyPath"
  }

  $req = Join-Path $PSScriptRoot "requirements.txt"
  if (Test-Path $req) {
    & uv pip install -r $req | Out-Null
  }

  return $pyPath
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function GhApiToFile {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$GhArgs,
    [Parameter(Mandatory = $true)]
    [string]$OutPath
  )
  $output = & gh @GhArgs
  [System.IO.File]::WriteAllText($OutPath, $output, $utf8NoBom)
}

Require-Command gh

# Ensure gh auth
try {
  & gh auth status -h github.com | Out-Null
} catch {
  throw "ERROR: gh is not authenticated. Run: gh auth login --hostname github.com --scopes repo"
}

if (-not $Env:GH_TOKEN -or $Env:GH_TOKEN.Trim() -eq "") {
  try {
    $Env:GH_TOKEN = (& gh auth token).Trim()
  } catch {
    # leave empty; downloads may fail for private assets
  }
}

New-Item -ItemType Directory -Force -Path $RawRoot | Out-Null

foreach ($repo in $Repos) {
  if ($repo -notmatch "/") {
    throw "ERROR: invalid repo format: $repo (expected OWNER/REPO)"
  }

  $parts = $repo.Split("/", 2)
  $owner = $parts[0]
  $name = $parts[1]
  $slug = "${owner}_${name}"
  $base = Join-Path $RawRoot $slug

  New-Item -ItemType Directory -Force -Path (Join-Path $base "issue_comments") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $base "pr_issue_comments") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $base "pr_review_comments") | Out-Null

  Write-Host "==> Fetching issues for $repo"
  $issuesPath = Join-Path $base "issues.json"
  GhApiToFile -GhArgs @("api", "-X", "GET", "repos/$owner/$name/issues", "-f", "state=all", "-f", "per_page=100", "--paginate") -OutPath $issuesPath

  Write-Host "==> Fetching PRs for $repo"
  $prsPath = Join-Path $base "prs.json"
  GhApiToFile -GhArgs @("api", "-X", "GET", "repos/$owner/$name/pulls", "-f", "state=all", "-f", "per_page=100", "--paginate") -OutPath $prsPath

  Write-Host "==> Fetching issue comments for $repo"
  $issues = Get-Content $issuesPath -Raw | ConvertFrom-Json
  foreach ($issue in $issues) {
    if ($issue.PSObject.Properties.Match("pull_request").Count -gt 0) { continue }
    $num = $issue.number
    $out = Join-Path (Join-Path $base "issue_comments") "ISSUE-$num.json"
    GhApiToFile -GhArgs @("api", "-X", "GET", "repos/$owner/$name/issues/$num/comments", "-f", "per_page=100", "--paginate") -OutPath $out
  }

  Write-Host "==> Fetching PR comments for $repo"
  $prs = Get-Content $prsPath -Raw | ConvertFrom-Json
  foreach ($pr in $prs) {
    $num = $pr.number
    $outIssue = Join-Path (Join-Path $base "pr_issue_comments") "PR-$num.json"
    GhApiToFile -GhArgs @("api", "-X", "GET", "repos/$owner/$name/issues/$num/comments", "-f", "per_page=100", "--paginate") -OutPath $outIssue

    $outReview = Join-Path (Join-Path $base "pr_review_comments") "PR-$num.json"
    GhApiToFile -GhArgs @("api", "-X", "GET", "repos/$owner/$name/pulls/$num/comments", "-f", "per_page=100", "--paginate") -OutPath $outReview
  }
}

$py = Ensure-UvVenv
$scriptPath = Join-Path $PSScriptRoot "export_issues_prs.py"

$repoArgs = @()
foreach ($repo in $Repos) {
  $repoArgs += "--repo"
  $repoArgs += $repo
}

$pyArgs = @($scriptPath, "--raw-root", $RawRoot, "--out-root", $OutRoot) + $repoArgs
& $py @pyArgs

Write-Host "Done. Output is in $OutRoot/"
