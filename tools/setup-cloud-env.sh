#!/usr/bin/env bash
# Bootstraps a fresh Linux environment (e.g. Claude Code cloud env) for this
# homelab repo. Idempotent — safe to re-run.
#
# Run from anywhere — the script cds to the repo root before doing work.
#
# Required env vars (read at Claude Code session time, not by this script):
#   BUILDBUDDY_API_KEY  - for the BuildBuddy MCP server (defined in .mcp.json)
#
# Optional env vars used by this script:
#   GITHUB_TOKEN        - if set, used to authenticate the gh CLI
#   HOMELAB_NO_SUDO     - set to 1 to force the user-local path below
#   HOMELAB_ENV_FILE    - where the user-local path writes its PATH exports
#                         (default: ${XDG_CACHE_HOME:-~/.cache}/homelab-env.sh)
#
# Two paths:
#   system      sudo + apt-get: installs crane and direnv system-wide, then
#               relies on direnv to load .envrc (the original behaviour).
#   user-local  no sudo or no direnv (e.g. a Claude cloud session): installs
#               crane and gh with `go install`, bazelisk with npm, extracts the
#               tools image via ./bootstrap.sh, and writes the PATH additions
#               .envrc would make to $HOMELAB_ENV_FILE for the caller to source.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> homelab cloud env setup ($REPO_ROOT)"

can_sudo() {
	[[ "$(id -u)" == "0" ]] || { command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; }
}

# The system path needs sudo for crane (when missing) and sudo + apt-get for
# direnv (when missing). Anything short of that takes the user-local path.
if [[ "${HOMELAB_NO_SUDO:-0}" == "1" ]]; then
	MODE=user
elif command -v direnv >/dev/null 2>&1 && command -v crane >/dev/null 2>&1; then
	MODE=system
elif can_sudo && command -v apt-get >/dev/null 2>&1; then
	MODE=system
else
	MODE=user
fi

if [[ "$MODE" == "user" ]]; then
	echo "==> No sudo/direnv: using the user-local path"
	TOOLS_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/homelab-tools"
	ENV_FILE="${HOMELAB_ENV_FILE:-${XDG_CACHE_HOME:-$HOME/.cache}/homelab-env.sh}"
	LOCAL_BIN="$HOME/.local/bin"
	mkdir -p "$LOCAL_BIN"
	USER_PATH="$LOCAL_BIN"
	if command -v go >/dev/null 2>&1; then
		GO_BIN="$(go env GOBIN)"
		[[ -n "$GO_BIN" ]] || GO_BIN="$(go env GOPATH)/bin"
		USER_PATH="$GO_BIN:$USER_PATH"
	fi
	NPM_PREFIX=""
	if command -v npm >/dev/null 2>&1; then
		NPM_PREFIX="$(npm config get prefix)"
		# A root-owned global prefix needs sudo; install under ~/.local instead.
		[[ -w "$NPM_PREFIX" ]] || NPM_PREFIX="$HOME/.local"
		USER_PATH="$NPM_PREFIX/bin:$USER_PATH"
	fi
	export PATH="$USER_PATH:$PATH"

	# crane: go install, else the release tarball into ~/.local/bin.
	CRANE_VERSION="${CRANE_VERSION:-v0.20.2}"
	if command -v crane >/dev/null 2>&1; then
		echo "==> crane already installed"
	elif command -v go >/dev/null 2>&1; then
		echo "==> Installing crane ${CRANE_VERSION} (go install)"
		go install "github.com/google/go-containerregistry/cmd/crane@${CRANE_VERSION}"
	else
		echo "==> Installing crane ${CRANE_VERSION} (release tarball)"
		case "$(uname -m)" in
		x86_64) CRANE_ARCH="x86_64" ;;
		aarch64 | arm64) CRANE_ARCH="arm64" ;;
		*)
			echo "ERROR: unsupported arch $(uname -m)" >&2
			exit 1
			;;
		esac
		curl -fsSL "https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_Linux_${CRANE_ARCH}.tar.gz" |
			tar -xz -C "$LOCAL_BIN" crane
	fi

	# gh: optional (the session may reach GitHub over REST only), so a failed
	# install warns rather than aborting setup.
	GH_VERSION="${GH_VERSION:-v2.101.0}"
	if command -v gh >/dev/null 2>&1; then
		echo "==> gh already installed"
	elif command -v go >/dev/null 2>&1; then
		echo "==> Installing gh ${GH_VERSION} (go install)"
		go install "github.com/cli/cli/v2/cmd/gh@${GH_VERSION}" ||
			echo "    WARNING: gh install failed; continuing without it"
	else
		echo "==> go not found; skipping gh"
	fi

	# bazelisk: optional, `ci` needs only bb from the tools image.
	if command -v bazelisk >/dev/null 2>&1; then
		echo "==> bazelisk already installed"
	elif [[ -n "$NPM_PREFIX" ]]; then
		echo "==> Installing bazelisk (npm, prefix $NPM_PREFIX)"
		npm install --global --prefix "$NPM_PREFIX" --no-fund --no-audit @bazel/bazelisk >/dev/null ||
			echo "    WARNING: bazelisk install failed; continuing without it"
	else
		echo "==> npm not found; skipping bazelisk"
	fi

	echo "==> Running ./bootstrap.sh"
	./bootstrap.sh

	# The PATH .envrc would build, plus the user-local install dirs.
	mkdir -p "$(dirname "$ENV_FILE")"
	cat >"$ENV_FILE" <<-EOF
		# Written by tools/setup-cloud-env.sh (user-local path). Source it:
		#   . "$ENV_FILE"
		export PATH="$REPO_ROOT/bazel/tools/ci:$TOOLS_DIR/usr/bin:$USER_PATH:\$PATH"
	EOF
	echo "==> Wrote PATH exports to $ENV_FILE"

	# shellcheck source=/dev/null
	. "$ENV_FILE"
	echo ""
	echo "==> Verifying tools"
	missing=0
	for tool in ci crane prettier eslint shellcheck bb; do
		if command -v "$tool" >/dev/null 2>&1; then
			echo "    $tool: OK"
		else
			echo "    $tool: NOT FOUND"
			missing=1
		fi
	done
	for tool in gh bazelisk; do
		command -v "$tool" >/dev/null 2>&1 && echo "    $tool: OK" || echo "    $tool: not installed (optional)"
	done
	[[ "$missing" == "0" ]] || exit 1

	echo ""
	echo "==> Setup complete. Load the tools into this shell with:"
	echo "    . \"$ENV_FILE\""
	exit 0
fi

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
# 3. Pull vendored tools (helm, prettier, gofumpt, ruff, etc.)
# ---------------------------------------------------------------------------
echo "==> Running ./bootstrap.sh"
./bootstrap.sh

# ---------------------------------------------------------------------------
# 4. direnv allow (per-path approval — required for .envrc to load)
# ---------------------------------------------------------------------------
echo "==> direnv allow"
direnv allow .

# ---------------------------------------------------------------------------
# 5. gh CLI auth (if GITHUB_TOKEN is set and not already authenticated)
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

		         Without it, read pr-checks logs through the no-auth
		         GetEventLogChunk fallback in docs/agents/ci-triage.md.
	EOF
fi

echo ""
echo "==> Setup complete."
echo "    Restart the shell (or 'eval \"\$(direnv hook bash)\"') so direnv loads .envrc."
