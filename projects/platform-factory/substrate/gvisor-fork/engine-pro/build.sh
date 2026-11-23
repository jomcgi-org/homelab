#!/usr/bin/env bash
# Build the Semgrep Pro engine image for the gVisor fork pool.
#
#   build.sh layer OUT_DIR                 wheel libs + compile + OUT_DIR/layer.tar
#   build.sh image OUT_DIR                 append the layer to the base: OUT_DIR/image.tar
#   build.sh push OUT_DIR REPOSITORY:TAG   the same, pushed to the registry
#
# `layer` needs the Pro engine and the rules in OUT_DIR/pro, which only
# fetch-pro.py writes, with the token. Nothing here uses the token, and every
# command stops when SEMGREP_APP_TOKEN is in the environment: the image is built
# with the placeholder token of pins.env. `layer` compiles inside the builder
# image of ../engine/pins.env with docker (COMPILE_CMD runs compile.sh another way).
# `image` and `push` need crane; `push` needs a prior login. The image is for
# linux/amd64 only.
set -euo pipefail

if [[ -n "${SEMGREP_APP_TOKEN:-}" ]]; then
	echo "build.sh: SEMGREP_APP_TOKEN is set; build without it" >&2
	exit 1
fi

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=../engine/pins.env
. "$here/../engine/pins.env"
# shellcheck source=pins.env
. "$here/pins.env"

cmd=${1:?usage: build.sh layer|image|push OUT_DIR [REPOSITORY:TAG]}
out=${2:?usage: build.sh layer|image|push OUT_DIR [REPOSITORY:TAG]}
mkdir -p "$out"
out=$(cd "$out" && pwd)

layer() {
	for f in semgrep-core-proprietary rules.json; do
		if [[ ! -s "$out/pro/$f" ]]; then
			echo "build.sh: $out/pro/$f is missing; run fetch-pro.py first" >&2
			exit 1
		fi
	done
	if [[ ! -s "$out/wheel.whl" ]]; then
		curl --fail --silent --show-error --location --output "$out/wheel.whl" "$WHEEL_URL"
	fi
	rm -rf "$out/bin"
	mkdir -p "$out/bin"
	if [[ -n "${COMPILE_CMD:-}" ]]; then
		$COMPILE_CMD "$out/bin"
	else
		docker run --rm --user "$(id -u):$(id -g)" \
			-v "$here/..:/src:ro" -v "$out/bin:/out" "$BUILDER_IMAGE" /src/engine-pro/compile.sh /out
	fi
	python3 -I "$here/make-layer.py" "$out/wheel.whl" "$out/bin" "$out/pro" "$out/layer.tar"
}

# One crane call appends the layer, sets the user and the labels. The base keeps
# its user 65532 and its environment; the template gives the command. There is
# no org.opencontainers.image.source label on purpose: it would link the package
# to the public repository, and the package must not inherit that access.
mutate() {
	local engine_sha rules_sha
	engine_sha=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["binary_sha256"])' "$out/pro/fetch.json")
	rules_sha=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["rules_sha256"])' "$out/pro/fetch.json")
	crane mutate --platform linux/amd64 --append "$out/layer.tar" \
		--user 65532:65532 --workdir / \
		--label "org.opencontainers.image.description=PRIVATE Semgrep Pro engine ${SEMGREP_VERSION} with the Secrets rules and the listen-after-fork server for the gVisor sandbox fork pool" \
		--label "org.opencontainers.image.licenses=LicenseRef-Semgrep-Proprietary" \
		--label "org.opencontainers.image.base.name=${BASE_IMAGE}" \
		--label "dev.jomcgi.semgrep.engine-sha256=${engine_sha}" \
		--label "dev.jomcgi.semgrep.rules-sha256=${rules_sha}" \
		"$@" "$BASE_IMAGE"
}

case "$cmd" in
layer) layer ;;
image) mutate --output "$out/image.tar" --tag "engine-pro:local" ;;
push) mutate --tag "${3:?usage: build.sh push OUT_DIR REPOSITORY:TAG}" ;;
*)
	echo "unknown command $cmd" >&2
	exit 64
	;;
esac
