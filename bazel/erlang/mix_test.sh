#!/usr/bin/env bash
# Smoke test: prove the prebuilt OTP + precompiled Elixir toolchain can compile
# and ExUnit-test the EmberVM control-plane skeleton ON the RBE executor,
# INCLUDING hermetically-staged hex dependencies. This is the other half of Task
# 1's toolchain (bin/erl was proved by otp_smoke).
#
# Args: $1 OTP Install script, $2 elixir bin/elixir anchor, $3 control mix.exs
#       anchor, $4 output marker, $5 hex.ez archive, $6 generated node.pb.ex,
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

# Absolutize the output before we cd away (Bazel passes it execroot-relative).
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

# Inject the generated node.proto Elixir stub into the app (mix globs lib/**).
# It is a build product kept out of the committed control/ (see the proto codegen
# genrule), so prod lib/ code that references Embervm.Node.V1.* (Task 9's
# NodeRegistry) only compiles once it is staged here, the same injection the
# round-trip test and the release build use.
mkdir -p "$work/control/lib/embervm/node/v1"
cp "$node_pb_ex" "$work/control/lib/embervm/node/v1/node.pb.ex"

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

# rebar3 for mix-built rebar deps (OTel gRPC exporter chain, Task 13): the
# genrule passes it absolute as MIX_REBAR3_SRC; stage onto the OTP bin (already on
# PATH) so mix finds it. Absent (the pre-OTel closure) leaves the build unchanged.
if [ -n "${MIX_REBAR3_SRC:-}" ]; then
	cp "$MIX_REBAR3_SRC" "$work/otp/bin/rebar3"
	chmod +x "$work/otp/bin/rebar3"
	export MIX_REBAR3="$work/otp/bin/rebar3"
fi

# Layer-1 spec vocabulary test inputs (ADR embervm/006): stage the spec sources
# (adoption.tla + vocabulary.exs) and node.proto into the sandbox and point the
# test at them via env, so spec_vocabulary_test.exs is hermetic on the executor.
# Both are OPTIONAL: absent (any lane not passing these) leaves the build
# unchanged, exactly like MIX_REBAR3_SRC above. Bazel passes the paths
# execroot-relative, so absolutize each before we cd into the workdir.
if [ -n "${EMBERVM_SPECS_SRCS:-}" ]; then
	mkdir -p "$work/specs"
	for spec in $EMBERVM_SPECS_SRCS; do
		case "$spec" in
		/*) abs="$spec" ;;
		*) abs="$(pwd)/$spec" ;;
		esac
		cp "$abs" "$work/specs/"
	done
	export EMBERVM_SPECS_DIR="$work/specs"
fi
if [ -n "${EMBERVM_NODE_PROTO_SRC:-}" ]; then
	case "$EMBERVM_NODE_PROTO_SRC" in
	/*) proto_abs="$EMBERVM_NODE_PROTO_SRC" ;;
	*) proto_abs="$(pwd)/$EMBERVM_NODE_PROTO_SRC" ;;
	esac
	mkdir -p "$work/proto"
	cp "$proto_abs" "$work/proto/node.proto"
	export EMBERVM_NODE_PROTO="$work/proto/node.proto"
fi
export HOME="$work" # mix/hex write under $HOME (~/.mix); keep it in the sandbox
export MIX_ENV=test
# The executor env is stripped, so force UTF-8 filename handling (avoids the
# latin1 native-encoding warning and any non-ASCII path mangling during compile).
export ELIXIR_ERL_OPTIONS="+fnu"
# Belt and suspenders: even with Hex registered, forbid any registry contact. The
# path overrides mean resolution is fully local, so Hex must never reach the net.
export HEX_OFFLINE=1

# Register Hex offline from the staged archive. mix needs the Hex SCM to parse the
# hex-style dep declarations inside our path deps (see repositories.bzl); this
# install is from a local .ez, so it reaches no network.
mix archive.install "$hex_ez" --force >&2

cd "$work/control"
# Compile the path deps FIRST (builds the exqlite NIF via make, generates each
# dep's .app). This is required: `mix test --no-deps-check` skips the deps
# freshness check and, as a side effect, will NOT compile uncompiled deps, so
# without this the app fails to start ("could not find application file:
# exqlite.app"). --no-deps-check keeps mix from auditing against a (nonexistent)
# lock or reaching hex.
mix deps.compile --no-deps-check >&2
# EXUNIT_SEED replays a specific run's test ordering. ExUnit prints the seed it chose
# ("Running ExUnit with seed: N"), but without this there was no way to feed one back,
# so a captured seed from a failing CI run could not be replayed (#4078). Note a seed
# only reproduces an ordering against the SAME test corpus: adding or removing a test
# changes the permutation that seed yields, which is why the commit is echoed with it.
seed_args=""
if [ -n "${EXUNIT_SEED:-}" ]; then
	seed_args="--seed ${EXUNIT_SEED}"
	echo "MIX TEST replaying with EXUNIT_SEED=${EXUNIT_SEED}" >&2
fi

if mix test --no-deps-check $seed_args >"$out" 2>&1; then
	echo "MIX TEST OK on the executor" >&2
	cat "$out" >&2
else
	echo "MIX TEST FAILED on the executor:" >&2
	cat "$out" >&2
	# Reproduction recipe on one line, otherwise buried in thousands. The seed alone is
	# NOT enough: ExUnit shuffles the actual test list, so the same seed over a different
	# corpus yields a different ordering and silently fails to reproduce. The test count
	# is a cheap corpus fingerprint (the commit is not available here: builds are not
	# stamped, deliberately, and the sandbox has no .git).
	echo "MIX TEST reproduce with: EXUNIT_SEED=$(grep -oE 'seed: [0-9]+' "$out" | head -1 | grep -oE '[0-9]+') against a $(grep -oE '[0-9]+ tests, [0-9]+ failures?' "$out" | tail -1 | grep -oE '^[0-9]+')-test corpus (a seed only replays the same ordering at the same test count)" >&2
	exit 1
fi
