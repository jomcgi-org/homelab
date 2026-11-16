#!/usr/bin/env bash
# Exercise the cache and activation boundary without public network access.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/repo/projects/factory/toolchain" "$fixture/repo/bazel/tools" "$fixture/bin"
cp "$SCRIPT_DIR/bootstrap.sh" "$SCRIPT_DIR/install.py" "$fixture/repo/projects/factory/toolchain/"
echo '{}' >"$fixture/repo/bazel/tools/tools.lock.json"
echo '{}' >"$fixture/repo/package.json"

cat >"$fixture/bin/cat" <<'MOCK'
#!/bin/bash
if [[ "$1" == /etc/alpine-release ]]; then
	echo 3.22.8
else
	exec /bin/cat "$@"
fi
MOCK
cat >"$fixture/bin/uname" <<'MOCK'
#!/bin/bash
case "$1" in
-s) echo Linux ;;
-m) echo x86_64 ;;
esac
MOCK
cat >"$fixture/bin/apk" <<'MOCK'
#!/bin/bash
set -euo pipefail
echo install >>"$TEST_INSTALL_LOG"
[[ "${TEST_FAIL_INSTALL:-}" != 1 ]] || exit 17
[[ "$1" == --root ]]
mkdir -p "$2/usr/bin" "$2/bin"
for executable in "$2/usr/bin/node" "$2/bin/grep"; do
	printf '#!/bin/bash\nexit 0\n' >"$executable"
	chmod 0755 "$executable"
done
cat >"$2/usr/bin/python3" <<'PYTHON'
#!/bin/bash
set -euo pipefail
[[ "$1" == */install.py ]] || exit 0
root="$3"
for tool in pnpm prettier ruff shfmt gofumpt buildifier crane shellcheck helm; do
	printf '#!/bin/bash\nexit 0\n' >"$root/bin/$tool"
	chmod 0755 "$root/bin/$tool"
done
PYTHON
chmod 0755 "$2/usr/bin/python3"
MOCK
chmod 0755 "$fixture/bin/"*
export PATH="$fixture/bin:$PATH"
export TEST_INSTALL_LOG="$fixture/install.log"
export XDG_CACHE_HOME="$fixture/cache with spaces"
bootstrap="$fixture/repo/projects/factory/toolchain/bootstrap.sh"
env_file="$XDG_CACHE_HOME/homelab-sandbox-tools/env.sh"

# Failed downloads do not publish an env file or a complete cache.
if TEST_FAIL_INSTALL=1 bash "$bootstrap"; then
	echo 'accepted a failed APK install' >&2
	exit 1
fi
test ! -e "$env_file"
bash "$bootstrap"
test "$(wc -l <"$TEST_INSTALL_LOG" | tr -d ' ')" = 2
# A restored snapshot can use a healthy cache without fetching again.
bash "$bootstrap"
test "$(wc -l <"$TEST_INSTALL_LOG" | tr -d ' ')" = 2
# shellcheck source=/dev/null
source "$env_file"
test "$TMPDIR" = "/var/tmp/homelab-sandbox-$(id -u)"
test -d "$TMPDIR"
temp_file="$(bash -c 'mktemp')"
test "${temp_file%/*}" = "$TMPDIR"
rm "$temp_file"
test -x "$(command -v helm)"
helm version --short

# Missing executables must cause a repair even when the inputs match.
rm "$(command -v helm)"
bash "$bootstrap"
test "$(wc -l <"$TEST_INSTALL_LOG" | tr -d ' ')" = 3
# shellcheck source=/dev/null
source "$env_file"
helm version --short

# An input change selects a new cache, and a failed refresh preserves the
# previously published environment.
cp "$env_file" "$fixture/previous-env"
echo '{"changed":true}' >"$fixture/repo/package.json"
if TEST_FAIL_INSTALL=1 bash "$bootstrap"; then
	exit 1
fi
cmp "$env_file" "$fixture/previous-env"
echo 'sandbox bootstrap cache tests passed'
