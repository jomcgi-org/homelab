#!/usr/bin/env bash
# Build the EmberVM control-plane OTP release with the prebuilt OTP + precompiled
# Elixir toolchain, ON the RBE executor, and emit it as a single deterministic
# tar with paths rooted at opt/embervm/ (ready to layer into the apko image).
#
# include_erts:false, so the release is architecture-independent .beam bytecode:
# one tar serves both arches, and the apko image supplies erlang at runtime.
#
# Args: $1 OTP Install script, $2 elixir bin/elixir anchor, $3 control mix.exs
#       anchor, $4 output tar.
set -euo pipefail

install_script="$1"
elixir_anchor="$2"
mixexs="$3"
out="$4"

case "$out" in
  /*) ;;
  *) out="$(pwd)/$out" ;;
esac

otp_src="$(cd "$(dirname "$install_script")" && pwd)"
elixir_src="$(cd "$(dirname "$elixir_anchor")/.." && pwd)"
control_src="$(cd "$(dirname "$mixexs")" && pwd)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cp -RL "$otp_src"/. "$work/otp/"
cp -RL "$elixir_src"/. "$work/elixir/"
cp -RL "$control_src"/. "$work/control/"

"$work/otp/Install" -minimal "$work/otp" >/dev/null

export PATH="$work/otp/bin:$work/elixir/bin:$PATH"
export HOME="$work"
export MIX_ENV=prod

cd "$work/control"
mix release --overwrite >&2

# Stage the release under opt/embervm and tar deterministically (fixed mtime,
# sorted names, root ownership) so the image layer is reproducible.
mkdir -p "$work/pkg/opt/embervm"
cp -RL "$work/control/_build/prod/rel/embervm"/. "$work/pkg/opt/embervm/"
cd "$work/pkg"
tar --sort=name --mtime='2020-01-01 00:00:00' --owner=0 --group=0 --numeric-owner -cf "$out" opt
echo "RELEASE OK: wrote $(du -h "$out" | cut -f1) release tar" >&2
