#!/usr/bin/env bash
# Bootstrap developer tools for the homelab repo.
# Detects host platform and pulls the matching variant from the multi-platform
# OCI tools image (linux/amd64, linux/arm64, darwin/arm64).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="$SCRIPT_DIR/.tools-version"
if [[ ! -f "$VERSION_FILE" ]]; then
	echo "ERROR: Missing .tools-version file in $SCRIPT_DIR"
	exit 1
fi
TOOLS_IMAGE="$(head -1 "$VERSION_FILE" | tr -d '[:space:]')"
TOOLS_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/homelab-tools"

# Detect platform for multi-platform image
case "$(uname -s)-$(uname -m)" in
Darwin-arm64) PLATFORM="darwin/arm64" ;;
Linux-x86_64) PLATFORM="linux/amd64" ;;
Linux-aarch64) PLATFORM="linux/arm64" ;;
*)
	echo "ERROR: Unsupported platform: $(uname -s)-$(uname -m)"
	exit 1
	;;
esac

# Install crane if missing (macOS only — Linux CI should have it)
if ! command -v crane &>/dev/null; then
	if [[ "$(uname -s)" == "Darwin" ]] && command -v brew &>/dev/null; then
		echo "Installing crane via Homebrew..."
		brew install crane
	else
		echo "ERROR: crane is required. Install from https://github.com/google/go-containerregistry"
		exit 1
	fi
fi

# Check remote digest for this platform
REMOTE_DIGEST=$(crane digest --platform "$PLATFORM" "$TOOLS_IMAGE" 2>/dev/null) || {
	echo "ERROR: Failed to fetch digest for $TOOLS_IMAGE ($PLATFORM)"
	echo "Check that the image exists and you have access to ghcr.io"
	exit 1
}

# Skip if already up to date
if [[ -f "$TOOLS_DIR/.digest" ]] && [[ "$(cat "$TOOLS_DIR/.digest")" == "$REMOTE_DIGEST" ]]; then
	echo "Tools already up to date ($REMOTE_DIGEST)"
	exit 0
fi

echo "Pulling developer tools ($PLATFORM) from $TOOLS_IMAGE..."
rm -rf "$TOOLS_DIR"
mkdir -p "$TOOLS_DIR"
crane export --platform "$PLATFORM" "$TOOLS_IMAGE" - |
	tar --no-same-owner -xf - -C "$TOOLS_DIR"
echo "$REMOTE_DIGEST" >"$TOOLS_DIR/.digest"

echo "Done. Run 'direnv allow' to add tools to PATH."
