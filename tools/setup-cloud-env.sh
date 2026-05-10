#!/usr/bin/env bash
# Bootstraps a fresh Linux environment (e.g. Claude Code cloud env) for this
# homelab repo. Idempotent — safe to re-run.
#
# Run from anywhere — the script cds to the repo root before doing work.
#
# Required env vars (read at Claude Code session time, not by this script):
#   BUILDBUDDY_API_KEY  - for the BuildBuddy MCP server (defined in .mcp.json)
#
# Required env vars (also used by this script):
#   GITHUB_TOKEN        - authenticates crane to ghcr.io (needed to pull the
#                         private tools image) and the gh CLI

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> homelab cloud env setup ($REPO_ROOT)"

# ---------------------------------------------------------------------------
# 1. crane (Linux only — bootstrap.sh installs it via brew on macOS)
# ---------------------------------------------------------------------------
if ! command -v crane >/dev/null 2>&1; then
	echo "==> Installing crane"
	CRANE_VERSION="${CRANE_VERSION:-v0.20.2}"
	case "$(uname -m)" in
	x86_64) CRANE_ARCH="x86_64" ;;
	aarch64 | arm64) CRANE_ARCH="arm64" ;;
	*)
		echo "ERROR: unsupported arch $(uname -m)" >&2
		exit 1
		;;
	esac
	curl -fsSL "https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_Linux_${CRANE_ARCH}.tar.gz" |
		sudo tar -xz -C /usr/local/bin crane
else
	echo "==> crane already installed"
fi

# ---------------------------------------------------------------------------
# 2. direnv (loads .envrc to put vendored tools on PATH)
# ---------------------------------------------------------------------------
if ! command -v direnv >/dev/null 2>&1; then
	echo "==> Installing direnv"
	if command -v apt-get >/dev/null 2>&1; then
		sudo apt-get update -qq && sudo apt-get install -y -qq direnv
	else
		echo "ERROR: apt-get not found; install direnv manually" >&2
		exit 1
	fi
	if ! grep -q 'direnv hook bash' "${HOME}/.bashrc" 2>/dev/null; then
		echo 'eval "$(direnv hook bash)"' >>"${HOME}/.bashrc"
		echo "    Added direnv hook to ~/.bashrc"
	fi
else
	echo "==> direnv already installed"
fi

# ---------------------------------------------------------------------------
# 3. Authenticate crane to ghcr.io (required to pull the private tools image)
# ---------------------------------------------------------------------------
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
	echo "==> Authenticating crane to ghcr.io"
	echo "$GITHUB_TOKEN" | crane auth login ghcr.io --username x-access-token --password-stdin
elif crane auth get ghcr.io >/dev/null 2>&1; then
	echo "==> crane already authenticated to ghcr.io"
else
	echo "WARNING: \$GITHUB_TOKEN is not set and crane has no cached ghcr.io credentials."
	echo "         bootstrap.sh may fail if the tools image is private."
fi

# ---------------------------------------------------------------------------
# 4. Pull vendored tools (helm, prettier, gofumpt, ruff, etc.)
# ---------------------------------------------------------------------------
echo "==> Running ./bootstrap.sh"
./bootstrap.sh

# ---------------------------------------------------------------------------
# 5. direnv allow (per-path approval — required for .envrc to load)
# ---------------------------------------------------------------------------
echo "==> direnv allow"
direnv allow .

# ---------------------------------------------------------------------------
# 6. gh CLI auth (if GITHUB_TOKEN is set and not already authenticated)
# ---------------------------------------------------------------------------
if command -v gh >/dev/null 2>&1; then
	if gh auth status >/dev/null 2>&1; then
		echo "==> gh CLI already authenticated"
	elif [[ -n "${GITHUB_TOKEN:-}" ]]; then
		echo "==> Authenticating gh CLI from \$GITHUB_TOKEN"
		echo "$GITHUB_TOKEN" | gh auth login --with-token
	else
		echo "==> gh CLI not authenticated and \$GITHUB_TOKEN unset; skipping"
	fi
else
	echo "==> gh CLI not installed; skipping auth"
fi

# ---------------------------------------------------------------------------
# Sanity check — vendored tools should be on PATH inside direnv
# ---------------------------------------------------------------------------
echo ""
echo "==> Verifying vendored tools (via direnv exec)"
if direnv exec . sh -c 'command -v format >/dev/null'; then
	echo "    format: OK"
else
	echo "    format: NOT FOUND — bootstrap may have failed"
	exit 1
fi

# ---------------------------------------------------------------------------
# BUILDBUDDY_API_KEY warning (required at Claude Code runtime, not by setup)
# ---------------------------------------------------------------------------
echo ""
if [[ -z "${BUILDBUDDY_API_KEY:-}" ]]; then
	cat <<-'EOF'
		WARNING: $BUILDBUDDY_API_KEY is not set in this shell.
		         The BuildBuddy MCP server (defined in .mcp.json) needs it at
		         Claude Code session start. Set it in your shell env, e.g.:

		           export BUILDBUDDY_API_KEY=<your-key>
	EOF
fi

echo ""
echo "==> Setup complete."
echo "    Restart the shell (or 'eval \"\$(direnv hook bash)\"') so direnv loads .envrc."
