$ErrorActionPreference = "Stop"
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
$OutRoot = if ($Env:EXPORT_OUT_ROOT -and $Env:EXPORT_OUT_ROOT.Trim() -ne "") { $Env:EXPORT_OUT_ROOT } else { "export" }

function Require-Command([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "ERROR: missing command: $Name"
  }
}

Require-Command uv

$venvPath = Join-Path $PSScriptRoot ".venv"
if (-not (Test-Path $venvPath)) {
  & uv venv $venvPath | Out-Null
}

$py = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $py)) {
  throw "ERROR: venv python not found at $py"
}

& $py (Join-Path $PSScriptRoot "cleanup_img_ext.py") --export-root $OutRoot --debug
