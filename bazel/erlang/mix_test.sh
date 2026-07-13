#!/usr/bin/env bash
# Smoke test: prove the prebuilt OTP + precompiled Elixir toolchain can compile
# and ExUnit-test the EmberVM control-plane skeleton ON the RBE executor. This is
# the other half of Task 1's toolchain (bin/erl was proved by otp_smoke).
#
# Args: $1 OTP Install script, $2 elixir bin/elixir anchor, $3 control mix.exs
#       anchor, $4 output marker.
set -euo pipefail

install_script="$1"
elixir_anchor="$2"
mixexs="$3"
out="$4"

# Absolutize the output before we cd away (Bazel passes it execroot-relative).
case "$out" in
  /*) ;;
  *) out="$(pwd)/$out" ;;
esac

otp_src="$(cd "$(dirname "$install_script")" && pwd)"
elixir_src="$(cd "$(dirname "$elixir_anchor")/.." && pwd)" # bin/elixir -> root
control_src="$(cd "$(dirname "$mixexs")" && pwd)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Stage everything writable: Install bakes ERL_ROOT, and mix writes _build/.
cp -RL "$otp_src"/. "$work/otp/"
cp -RL "$elixir_src"/. "$work/elixir/"
cp -RL "$control_src"/. "$work/control/"

"$work/otp/Install" -minimal "$work/otp" >/dev/null

export PATH="$work/otp/bin:$work/elixir/bin:$PATH"
export HOME="$work"       # mix/hex write under $HOME; keep it in the sandbox
export MIX_ENV=test

cd "$work/control"
# Zero hex deps by design, so no network is needed. --no-deps-check keeps mix
# from trying to reach hex for a deps audit.
if mix test --no-deps-check > "$out" 2>&1; then
  echo "MIX TEST OK on the executor" >&2
  cat "$out" >&2
else
  echo "MIX TEST FAILED on the executor:" >&2
  cat "$out" >&2
  exit 1
fi
