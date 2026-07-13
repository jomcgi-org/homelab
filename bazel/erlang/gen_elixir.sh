#!/usr/bin/env bash
# Elixir codegen for node.proto: build the protoc-gen-elixir escript from the
# protobuf hex package under the prebuilt OTP + Elixir toolchain, then run protoc
# with plugins=grpc to emit node.pb.ex. Runs on the RBE executor with no network
# (protobuf is staged from its pinned hex tarball as a `path:` dep). This is the
# Elixir half of the //projects/embervm/proto/embervm/node/v1 codegen; the Go
# half is a plain protoc genrule (no OTP needed).
#
# Args: $1 OTP Install script, $2 elixir bin/elixir anchor, $3 protoc binary,
#       $4 wrapper mix.exs (protoc_gen_elixir_mix.exs), $5 hex.ez archive,
#       $6 protobuf hex tarball, $7 node.proto, $8 output .pb.ex path.
set -euo pipefail

install_script="$1"
elixir_anchor="$2"
protoc="$3"
mixexs="$4"
hex_ez="$5"
protobuf_tarball="$6"
proto="$7"
out="$8"

# Absolutize every input Bazel hands us execroot-relative, before we cd away.
abspath() {
	case "$1" in
	/*) printf '%s' "$1" ;;
	*) printf '%s/%s' "$(pwd)" "$1" ;;
	esac
}
protoc="$(abspath "$protoc")"
mixexs="$(abspath "$mixexs")"
hex_ez="$(abspath "$hex_ez")"
protobuf_tarball="$(abspath "$protobuf_tarball")"
proto="$(abspath "$proto")"
out="$(abspath "$out")"

otp_src="$(cd "$(dirname "$install_script")" && pwd)"
elixir_src="$(cd "$(dirname "$elixir_anchor")/.." && pwd)" # bin/elixir -> root

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Stage everything writable: Install bakes ERL_ROOT, and mix writes _build/.
cp -RL "$otp_src"/. "$work/otp/"
cp -RL "$elixir_src"/. "$work/elixir/"

# The codegen project: the wrapper mix.exs plus protobuf unpacked as a path dep.
# Hex tarballs nest the source inside contents.tar.gz (see repositories.bzl).
mkdir -p "$work/proj/deps/protobuf"
cp "$mixexs" "$work/proj/mix.exs"
inner="$(mktemp -d)"
tar xf "$protobuf_tarball" -C "$inner"
tar xzf "$inner/contents.tar.gz" -C "$work/proj/deps/protobuf"
rm -rf "$inner"

"$work/otp/Install" -minimal "$work/otp" >/dev/null

export PATH="$work/otp/bin:$work/elixir/bin:$PATH"
export HOME="$work"              # mix/hex write under $HOME (~/.mix); keep it in the sandbox
export MIX_ENV=prod              # keep protobuf's dev/test-only deps (credo, dialyxir) out
export ELIXIR_ERL_OPTIONS="+fnu" # force UTF-8 filename handling on a stripped env
export HEX_OFFLINE=1             # never reach the registry; path deps make resolution local

# Register Hex offline so mix can parse the hex-style dep declarations inside
# protobuf's own mix.exs (same reason the control build needs it). Local .ez, no
# network.
mix archive.install "$hex_ez" --force >&2

cd "$work/proj"
# Compile the protobuf path dep, then build the escript. --no-deps-check skips
# the (nonexistent) lock/freshness audit; deps.compile is required because
# escript.build will not compile an uncompiled path dep on its own.
mix deps.compile --no-deps-check >&2
mix escript.build >&2

escript_bin="$work/proj/protoc-gen-elixir"
chmod +x "$escript_bin"

# Run protoc. The proto's canonical name must be embervm/node/v1/node.proto, so
# -I is the proto root (its path with that suffix stripped). plugins=grpc makes
# protoc-gen-elixir emit the Service/Stub modules alongside the messages. protoc
# execs the escript, whose `#!/usr/bin/env escript` shebang resolves escript from
# the OTP bin we put on PATH above.
proto_root="${proto%/embervm/node/v1/node.proto}"
gen_out="$work/gen"
mkdir -p "$gen_out"
"$protoc" \
	--plugin=protoc-gen-elixir="$escript_bin" \
	--elixir_out=plugins=grpc:"$gen_out" \
	-I "$proto_root" \
	"$proto" >&2

# protoc-gen-elixir mirrors the proto's directory structure; there is exactly one
# generated file. Copy it to the declared output.
generated="$(find "$gen_out" -name '*.pb.ex' -type f)"
if [ "$(printf '%s\n' "$generated" | grep -c .)" != "1" ]; then
	echo "gen_elixir: expected exactly one .pb.ex, got:" >&2
	printf '%s\n' "$generated" >&2
	exit 1
fi
cp "$generated" "$out"
echo "gen_elixir: wrote $out" >&2
