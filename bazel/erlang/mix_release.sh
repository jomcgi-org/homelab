#!/usr/bin/env bash
# Build the EmberVM control-plane OTP release with the prebuilt OTP + precompiled
# Elixir toolchain, ON the RBE executor, and emit it as a single deterministic
# tar with paths rooted at opt/embervm/ (ready to layer into the apko image).
#
# The .beam bytecode is architecture-independent (include_erts:false), BUT once a
# hex dep ships a NIF (exqlite's sqlite3_nif.so) the release carries a compiled,
# arch-specific object. The RBE executor is amd64 and every cluster node is amd64,
# so this amd64 release is correct for the deployment; the embervm apko image is
# pinned amd64-only for the same reason (see projects/embervm/image/apko.yaml). If
# an arm64 node ever joins, this must become a per-arch build (the bazel/ocaml
# pattern).
#
# Args: $1 OTP Install script, $2 elixir bin/elixir anchor, $3 control mix.exs
#       anchor, $4 output tar, $5 hex.ez archive, $6 generated node.pb.ex,
#       $7.. hex dependency tarballs (may be empty).
set -euo pipefail

install_script="$1"
elixir_anchor="$2"
mixexs="$3"
out="$4"
hex_ez="$5"
node_pb_ex="$6"
shift 6
# Remaining args ("$@") are hex dependency tarballs.

case "$out" in
/*) ;;
*) out="$(pwd)/$out" ;;
esac
case "$hex_ez" in
/*) ;;
*) hex_ez="$(pwd)/$hex_ez" ;;
esac
case "$node_pb_ex" in
/*) ;;
*) node_pb_ex="$(pwd)/$node_pb_ex" ;;
esac

otp_src="$(cd "$(dirname "$install_script")" && pwd)"
elixir_src="$(cd "$(dirname "$elixir_anchor")/.." && pwd)"
control_src="$(cd "$(dirname "$mixexs")" && pwd)"

hex_tarballs=()
for tarball in "$@"; do
	case "$tarball" in
	/*) hex_tarballs+=("$tarball") ;;
	*) hex_tarballs+=("$(pwd)/$tarball") ;;
	esac
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cp -RL "$otp_src"/. "$work/otp/"
cp -RL "$elixir_src"/. "$work/elixir/"
cp -RL "$control_src"/. "$work/control/"

# Inject the generated node.proto Elixir stub into the app (mix globs lib/**), so
# Embervm.NodeRegistry (which references Embervm.Node.V1.*) compiles into the
# release. Build product kept out of committed control/; staged here exactly as
# mix_test.sh and the round-trip test stage it.
mkdir -p "$work/control/lib/embervm/node/v1"
cp "$node_pb_ex" "$work/control/lib/embervm/node/v1/node.pb.ex"

# Unpack each hex dependency tarball into deps/<name>/ (see mix_test.sh for the
# path-dep rationale). exqlite compiles its bundled sqlite3.c here (force_build in
# config/config.exs), so the executor's cc/make produce the amd64 NIF.
mkdir -p "$work/control/deps"
for abs in "${hex_tarballs[@]}"; do
	base="$(basename "$abs" .tar)"
	name="${base%-[0-9]*}"
	dest="$work/control/deps/$name"
	mkdir -p "$dest"
	inner="$(mktemp -d)"
	tar xf "$abs" -C "$inner"
	tar xzf "$inner/contents.tar.gz" -C "$dest"
	rm -rf "$inner"
done

"$work/otp/Install" -minimal "$work/otp" >/dev/null

export PATH="$work/otp/bin:$work/elixir/bin:$PATH"

# rebar3 for mix-built rebar deps (OTel gRPC exporter chain, Task 13): the
# genrule passes it absolute as MIX_REBAR3_SRC; stage onto the OTP bin (already on
# PATH) so mix finds it. Absent (the pre-OTel closure) leaves the build unchanged.
if [ -n "${MIX_REBAR3_SRC:-}" ]; then
	cp "$MIX_REBAR3_SRC" "$work/otp/bin/rebar3"
	chmod +x "$work/otp/bin/rebar3"
fi
export HOME="$work"
export MIX_ENV=prod
export ELIXIR_ERL_OPTIONS="+fnu"
export HEX_OFFLINE=1

# Register Hex offline from the staged archive (needed to parse hex-style dep
# declarations inside our path deps; see mix_test.sh). Local .ez, no network.
mix archive.install "$hex_ez" --force >&2

cd "$work/control"
# Compile path deps first (builds the exqlite NIF via make), then assemble the
# release. --no-deps-check avoids any lock/hex audit.
mix deps.compile --no-deps-check >&2
mix release --overwrite >&2

# Stage the release under opt/embervm and tar deterministically (fixed mtime,
# sorted names, root ownership) so the image layer is reproducible.
mkdir -p "$work/pkg/opt/embervm"
cp -RL "$work/control/_build/prod/rel/embervm"/. "$work/pkg/opt/embervm/"
cd "$work/pkg"
tar --sort=name --mtime='2020-01-01 00:00:00' --owner=0 --group=0 --numeric-owner -cf "$out" opt
echo "RELEASE OK: wrote $(du -h "$out" | cut -f1) release tar" >&2
