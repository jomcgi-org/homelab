#!/usr/bin/env bash
# Smoke test: prove the prebuilt ubuntu-22.04 OTP actually boots and can run
# crypto ON the RBE executor. The OTP tree is staged read-only, so copy it to a
# writable dir, bake ERL_ROOT with `Install -minimal`, then run erl.
#
# Args: $1 = path to the staged Install script, $2 = output marker file.
set -euo pipefail

install_script="$1"
out="$2"

# Bazel passes $out relative to the execroot (cwd at start); absolutize it before
# we cd into the work dir, or the later `tee "$out"` writes to the wrong place.
case "$out" in
  /*) ;;
  *) out="$(pwd)/$out" ;;
esac

root="$(cd "$(dirname "$install_script")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Copy the staged OTP tree (dereference symlinks; the sandbox tree is read-only).
cp -RL "$root"/. "$work"/
cd "$work"

# Bake ERL_ROOT to the working copy so bin/erl resolves its libs.
./Install -minimal "$work" >/dev/null

otp_release="$(./bin/erl -noshell -eval 'io:format("~s", [erlang:system_info(otp_release)]), halt().')"
# crypto:strong_rand_bytes loads the crypto NIF (libcrypto). If libssl/libcrypto
# is missing on the executor, this raises and the byte size is not 16.
crypto_bytes="$(./bin/erl -noshell -eval 'io:format("~w", [byte_size(crypto:strong_rand_bytes(16))]), halt().' 2>&1 || true)"

printf 'otp_release=%s crypto_bytes=%s\n' "$otp_release" "$crypto_bytes" | tee "$out"

if [ "$crypto_bytes" != "16" ]; then
	echo "SMOKE FAILED: crypto app did not load on the executor (crypto_bytes=$crypto_bytes)" >&2
	exit 1
fi
echo "SMOKE OK: prebuilt OTP $otp_release runs with working crypto on the executor" >&2
