#!/usr/bin/env bash
# Cross-language node.proto round-trip test: compile the control app WITH the
# generated Elixir stub (node.pb.ex) and the round-trip ExUnit test, stage the Go
# fake NodeService server binary, and run `mix test` against it on the RBE
# executor. This is the Task 3 acceptance check: the Go server stubs and the
# Elixir client stubs interoperate on the wire, including the server-streaming
# WatchNode RPC. No Firecracker involved.
#
# It extends the mix_test.sh staging (prebuilt OTP + Elixir + hermetic hex
# tarballs as path deps) with three extra inputs, so the general control test run
# (mix_test.sh) stays untouched and never needs the Go binary or the node stub.
#
# Args: $1 OTP Install script, $2 elixir bin/elixir anchor, $3 control mix.exs
#       anchor, $4 output marker, $5 hex.ez archive, $6 generated node.pb.ex,
#       $7 round-trip test .exs, $8 Go fake-node binary, $9.. hex dependency
#       tarballs.
set -euo pipefail

install_script="$1"
elixir_anchor="$2"
mixexs="$3"
out="$4"
hex_ez="$5"
node_pb_ex="$6"
test_exs="$7"
fake_node_bin="$8"
shift 8
# Remaining args ("$@") are hex dependency tarballs.

abspath() {
	case "$1" in
	/*) printf '%s' "$1" ;;
	*) printf '%s/%s' "$(pwd)" "$1" ;;
	esac
}
out="$(abspath "$out")"
hex_ez="$(abspath "$hex_ez")"
node_pb_ex="$(abspath "$node_pb_ex")"
test_exs="$(abspath "$test_exs")"
fake_node_bin="$(abspath "$fake_node_bin")"

otp_src="$(cd "$(dirname "$install_script")" && pwd)"
elixir_src="$(cd "$(dirname "$elixir_anchor")/.." && pwd)" # bin/elixir -> root
control_src="$(cd "$(dirname "$mixexs")" && pwd)"

hex_tarballs=()
for tarball in "$@"; do
	hex_tarballs+=("$(abspath "$tarball")")
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Stage everything writable: Install bakes ERL_ROOT, and mix writes _build/.
cp -RL "$otp_src"/. "$work/otp/"
cp -RL "$elixir_src"/. "$work/elixir/"
cp -RL "$control_src"/. "$work/control/"

# Inject the generated Elixir stub into the app (mix globs lib/**) and the
# round-trip test into the test tree. Both are build products kept out of the
# committed control/ so they only ever enter here.
mkdir -p "$work/control/lib/embervm/node/v1"
cp "$node_pb_ex" "$work/control/lib/embervm/node/v1/node.pb.ex"
mkdir -p "$work/control/test/embervm"
cp "$test_exs" "$work/control/test/embervm/node_roundtrip_test.exs"

# Unpack each hex dependency tarball into deps/<name>/ (Path SCM; see mix_test.sh).
mkdir -p "$work/control/deps"
for abs in "${hex_tarballs[@]}"; do
	base="$(basename "$abs" .tar)" # e.g. grpc-1.0.2
	name="${base%-[0-9]*}"         # strip -<version> -> grpc
	dest="$work/control/deps/$name"
	mkdir -p "$dest"
	inner="$(mktemp -d)"
	tar xf "$abs" -C "$inner"
	tar xzf "$inner/contents.tar.gz" -C "$dest"
	rm -rf "$inner"
done

"$work/otp/Install" -minimal "$work/otp" >/dev/null

export PATH="$work/otp/bin:$work/elixir/bin:$PATH"
export HOME="$work"
export MIX_ENV=test
export ELIXIR_ERL_OPTIONS="+fnu"
export HEX_OFFLINE=1
# The test spawns this Go binary and reads the port it prints.
cp "$fake_node_bin" "$work/fakenode"
chmod +x "$work/fakenode"
export EMBERVM_FAKE_NODE_BIN="$work/fakenode"

mix archive.install "$hex_ez" --force >&2

cd "$work/control"
# Compile the path deps first (see mix_test.sh), then run only the round-trip
# test file (not the general control suite).
mix deps.compile --no-deps-check >&2
if mix test --no-deps-check test/embervm/node_roundtrip_test.exs >"$out" 2>&1; then
	echo "NODE ROUND-TRIP OK on the executor" >&2
	cat "$out" >&2
else
	echo "NODE ROUND-TRIP FAILED on the executor:" >&2
	cat "$out" >&2
	exit 1
fi
