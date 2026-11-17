#!/usr/bin/env bash
# Rootless local checks for the Alpine 3.22 Codex harness. No remote credentials.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ "$(uname -s)-$(uname -m)" != Linux-x86_64 ]] ||
	[[ "$(cat /etc/alpine-release 2>/dev/null)" != 3.22.* ]]; then
	echo "sandbox bootstrap requires the Alpine 3.22 amd64 Codex harness" >&2
	exit 1
fi

TOOLS_BASE="${XDG_CACHE_HOME:-$HOME/.cache}/homelab-sandbox-tools"
mkdir -p "$TOOLS_BASE"
# /tmp is a 4 GiB tmpfs; /data can also be tmpfs in source guests.
# /var/tmp is on the writable root overlay instead.
TOOLS_TMPDIR="/var/tmp/homelab-sandbox-$(id -u)"
# Only the final per-user directory needs private permissions.
# shellcheck disable=SC2174
mkdir -p -m 0700 "$TOOLS_TMPDIR"
export TMPDIR="$TOOLS_TMPDIR"
fingerprint="$(cat "$SCRIPT_DIR/bootstrap.sh" "$SCRIPT_DIR/install.py" \
	"$REPO_ROOT/bazel/tools/tools.lock.json" "$REPO_ROOT/package.json" | sha256sum)"
TOOLS_ROOT="$TOOLS_BASE/${fingerprint%% *}"

smoke() {
	local root="$1" tool
	for tool in node pnpm prettier ruff shfmt gofumpt buildifier shellcheck; do
		"$root/bin/$tool" --version >/dev/null || return 1
	done
	"$root/bin/helm" version --short >/dev/null || return 1
	"$root/bin/crane" version >/dev/null || return 1
	"$root/bin/grep" --version >/dev/null || return 1
	"$root/bin/python3" -c 'import pytest, yaml' || return 1
	"$root/bin/pytest" --version >/dev/null || return 1
	"$root/bin/node" -e 'if (!process.versions.node.startsWith("22.")) process.exit(1); new Intl.Collator("en")' || return 1
	printf '<h1>{1+2}</h1>\n' |
		"$root/bin/prettier" --config "$REPO_ROOT/bazel/tools/format/prettier.config.cjs" \
			--parser svelte >/dev/null || return 1
}

if [[ ! -f "$TOOLS_ROOT/.complete" ]] || ! smoke "$TOOLS_ROOT"; then
	stage="$(mktemp -d "$TOOLS_BASE/.install.XXXXXX")"
	trap 'rm -rf "$stage"' EXIT
	# APK verifies the indexes and packages with the harness image's Alpine
	# keys. Install in a private root without chown, hooks or root privileges.
	# The v3.22 repositories supply security fixes within the Node 22 stream.
	printf '%s\n' \
		https://dl-cdn.alpinelinux.org/alpine/v3.22/main \
		https://dl-cdn.alpinelinux.org/alpine/v3.22/community >"$stage/repositories"
	apk --root "$stage/runtime" --initdb --no-cache --no-scripts --no-commit-hooks \
		--keys-dir /etc/apk/keys --repositories-file "$stage/repositories" \
		--arch x86_64 add --no-chown 'nodejs~22' npm python3 py3-pip py3-pytest py3-yaml grep
	mkdir -p "$stage/bin"
	# Use the APK root's libraries only for its own executables. Do not change
	# the harness/git library search path or replace its CA certificates.
	for tool in node python3 grep; do
		cat >"$stage/bin/$tool" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export LD_LIBRARY_PATH="$root/runtime/usr/lib:$root/runtime/lib"
if [[ "$(basename "$0")" == node ]]; then
	# Alpine's ICU data must resolve inside this relocated runtime.
	icu_data=("$root"/runtime/usr/share/icu/*)
	export ICU_DATA="${icu_data[0]}"
fi
binary="$root/runtime/usr/bin/$(basename "$0")"
if [[ ! -x "$binary" ]]; then
	# Alpine packages GNU grep under /bin.
	binary="$root/runtime/bin/$(basename "$0")"
fi
exec "$binary" "$@"
WRAPPER
		chmod 0755 "$stage/bin/$tool"
	done
	# Alpine installs pytest as a module only; the prompt and docs call the
	# pytest command, so run the module with the runtime's Python.
	cat >"$stage/bin/pytest" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/python3" -m pytest "$@"
WRAPPER
	chmod 0755 "$stage/bin/pytest"
	PATH="$stage/bin:$PATH" "$stage/bin/python3" "$SCRIPT_DIR/install.py" "$REPO_ROOT" "$stage"
	smoke "$stage"
	touch "$stage/.complete"
	# A failed refresh never becomes the active cache. Each actor has its own
	# writable home; bootstrap runs once before work starts.
	rm -rf "$TOOLS_ROOT"
	mv "$stage" "$TOOLS_ROOT"
	trap - EXIT
fi

# Source this in each command shell; no direnv or shell startup edits needed.
# Keep PATH expansion for the shell that sources env.sh.
# shellcheck disable=SC2016
printf 'export PATH=%q:"$PATH"\n' "$TOOLS_ROOT/bin:$REPO_ROOT/bazel/tools/ci" >"$TOOLS_BASE/env.sh"
printf 'export TMPDIR=%q\n' "$TOOLS_TMPDIR" >>"$TOOLS_BASE/env.sh"
echo "Local tools ready. Source $TOOLS_BASE/env.sh in each shell."
echo "Run ci lint, ci regen, ci ratchet and scoped helm/pytest checks; PR CI runs remote tests."
