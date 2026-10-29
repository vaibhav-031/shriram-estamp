#!/usr/bin/env bash
# One-time machine setup on macOS (Windows: setup.ps1). Safe to re-run – every step skips what is already there.
#
#   ./setup.sh
#
# Installs the tools only: Xcode Command Line Tools (git, make), Homebrew, uv, Python 3.13 + all deps (.venv),
# Postgres.app. It never touches Drive or the database – `login`, `init-db` and `check-setup` are listed at the
# end for you to run once .env and secrets/ are filled in.
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [ "$(uname -s)" != "Darwin" ]; then
	echo "setup.sh is for macOS. On Windows run setup.ps1 in PowerShell." >&2
	exit 1
fi

step "Xcode Command Line Tools (git, make)"
if xcode-select -p >/dev/null 2>&1; then
	echo "already installed"
else
	xcode-select --install || true
	echo "Finish the installer window that just opened, then run ./setup.sh again."
	exit 1
fi

step "Homebrew"
if ! command -v brew >/dev/null 2>&1; then
	/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
# A fresh install isn't on PATH yet in this shell.
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
	[ -x "$b" ] && eval "$("$b" shellenv)" && break
done
brew --version | head -1

step "uv"
command -v uv >/dev/null 2>&1 || brew install uv
uv --version

step "Python $(cat .python-version) + dependencies (.venv)"
uv sync   # downloads the pinned Python itself if it is missing

step "Postgres.app"
if [ -d /Applications/Postgres.app ]; then
	echo "already installed"
else
	brew install --cask postgres-app
	echo "Open Postgres.app → Initialize, then Server Settings → port 3000."
fi

step ".env"
if [ -f .env ]; then
	echo "already exists – left as is"
else
	cp .env.example .env
	echo "created from .env.example – fill in ESTAMP_DB_URL and the API keys"
fi
chmod 600 .env   # owner-only: run/run-once refuse a group/other-readable .env

step "secrets/"
mkdir -p secrets
if [ -f secrets/client_secret.json ]; then
	echo "client_secret.json present"
else
	echo "MISSING secrets/client_secret.json – download the OAuth Desktop client JSON (README §1)"
fi

cat <<'EOF'

Tools are installed. Next (README §1) – these talk to Google / Postgres, so run them yourself:
  make login         # Google sign-in → secrets/token.json
  make init-db       # create the ledger schema in ESTAMP_DB_URL
  make check-setup   # verify DB, sign-in, folders, config
Run `make` to list every task.
EOF
