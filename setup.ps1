# One-time machine setup on Windows 10/11 (macOS: setup.sh). Safe to re-run - every step skips what is already there.
#
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# Installs the tools only: Git, uv, Python 3.13 + all deps (.venv), PostgreSQL. It never touches Drive or the
# database - `login`, `init-db` and `check-setup` are listed at the end for you to run once .env and secrets\ are
# filled in. The Makefile is macOS-only; on Windows run the same tasks as `uv run python -m estamp <command>`.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
# winget installs don't update PATH in the running session.
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

if (-not (Have winget)) {
    throw "winget not found. Install 'App Installer' from the Microsoft Store, then run setup.ps1 again."
}

Step "Git"
if (Have git) { "already installed" } else {
    winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
    Refresh-Path
}

Step "uv"
if (-not (Have uv)) {
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    Refresh-Path
}
uv --version

Step "Python $(Get-Content .python-version) + dependencies (.venv)"
uv sync   # downloads the pinned Python itself if it is missing
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

Step "PostgreSQL"
$pg = Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue
if ($pg) { "already installed ($($pg[0].Name))" } else {
    winget install --id PostgreSQL.PostgreSQL.16 -e --accept-source-agreements --accept-package-agreements
    "Installer asks for a password for user 'postgres' and a port - pick 3000 to match the Mac setup."
}

Step ".env"
if (Test-Path .env) { "already exists - left as is" } else {
    Copy-Item .env.example .env
    "created from .env.example - fill in ESTAMP_DB_URL (postgresql://postgres:<password>@localhost:3000/postgres) and the API keys"
}

Step "secrets\"
New-Item -ItemType Directory -Force secrets | Out-Null
if (Test-Path secrets\client_secret.json) { "client_secret.json present" } else {
    "MISSING secrets\client_secret.json - download the OAuth Desktop client JSON (README section 1)"
}

@"

Tools are installed. Next (README section 1) - these talk to Google / Postgres, so run them yourself:
  uv run python -m estamp login         # Google sign-in -> secrets\token.json
  uv run python -m estamp init-db       # create the ledger schema in ESTAMP_DB_URL
  uv run python -m estamp check-setup   # verify DB, sign-in, folders, config
"@
