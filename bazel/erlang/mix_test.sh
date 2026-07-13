#!/usr/bin/env bash
# Smoke test: prove the prebuilt OTP + precompiled Elixir toolchain can compile
# and ExUnit-test the EmberVM control-plane skeleton ON the RBE executor,
# INCLUDING hermetically-staged hex dependencies. This is the other half of Task
# 1's toolchain (bin/erl was proved by otp_smoke).
#
# Args: $1 OTP Install script, $2 elixir bin/elixir anchor, $3 control mix.exs
#       anchor, $4 output marker, $5.. hex dependency tarballs (may be empty).
set -euo pipefail

install_script="$1"
elixir_anchor="$2"
mixexs="$3"
out="$4"
shift 4
# Remaining args ("$@") are hex dependency tarballs.

# Absolutize the output before we cd away (Bazel passes it execroot-relative).
case "$out" in
/*) ;;
*) out="$(pwd)/$out" ;;
esac

otp_src="$(cd "$(dirname "$install_script")" && pwd)"
elixir_src="$(cd "$(dirname "$elixir_anchor")/.." && pwd)" # bin/elixir -> root
control_src="$(cd "$(dirname "$mixexs")" && pwd)"

# Absolutize the tarball paths (Bazel passes them execroot-relative) before cd.
hex_tarballs=()
for tarball in "$@"; do
	case "$tarball" in
	/*) hex_tarballs+=("$tarball") ;;
	*) hex_tarballs+=("$(pwd)/$tarball") ;;
	esac
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Stage everything writable: Install bakes ERL_ROOT, and mix writes _build/.
cp -RL "$otp_src"/. "$work/otp/"
cp -RL "$elixir_src"/. "$work/elixir/"
cp -RL "$control_src"/. "$work/control/"

# Unpack each hex dependency tarball into deps/<name>/. Hex tarballs nest the
# source inside contents.tar.gz; the control mix.exs consumes each as a `path:`
# dep, so mix uses the Path SCM and never contacts hex.pm (no mix.lock, no
# `mix deps.get`).
mkdir -p "$work/control/deps"
for abs in "${hex_tarballs[@]}"; do
	base="$(basename "$abs" .tar)" # e.g. exqlite-0.38.0
	name="${base%-[0-9]*}"         # strip -<version> -> exqlite
	dest="$work/control/deps/$name"
	mkdir -p "$dest"
	inner="$(mktemp -d)"
	tar xf "$abs" -C "$inner" # -> VERSION CHECKSUM metadata.config contents.tar.gz
	tar xzf "$inner/contents.tar.gz" -C "$dest"
	rm -rf "$inner"
done

"$work/otp/Install" -minimal "$work/otp" >/dev/null

export PATH="$work/otp/bin:$work/elixir/bin:$PATH"
export HOME="$work" # mix/hex write under $HOME; keep it in the sandbox
export MIX_ENV=test

cd "$work/control"
# Deps are staged as path deps, so no network is needed. --no-deps-check keeps
# mix from auditing deps against a (nonexistent) lock or reaching hex.
if mix test --no-deps-check >"$out" 2>&1; then
	echo "MIX TEST OK on the executor" >&2
	cat "$out" >&2
else
	echo "MIX TEST FAILED on the executor:" >&2
	cat "$out" >&2
	exit 1
fi
