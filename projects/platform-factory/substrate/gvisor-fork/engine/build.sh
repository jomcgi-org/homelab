#!/usr/bin/env bash
# Build the Semgrep OSS engine image for the gVisor fork conformance Job.
#
#   build.sh layer OUT_DIR              fetch the wheel, compile, write OUT_DIR/layer.tar
#   build.sh image OUT_DIR              append the layer to the base: OUT_DIR/image.tar
#   build.sh push OUT_DIR REPOSITORY:TAG   the same, pushed to the registry
#
# `layer` compiles inside the builder image of pins.env with docker. Set
# COMPILE_CMD to run compile.sh another way (it gets the output directory as its
# argument). `image` and `push` need crane. `push` needs a prior login.
# Every input is pinned in pins.env; nothing here reads the network at runtime of
# the image. The image is for linux/amd64 only.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=pins.env
. "$here/pins.env"

cmd=${1:?usage: build.sh layer|image|push OUT_DIR [REPOSITORY:TAG]}
out=${2:?usage: build.sh layer|image|push OUT_DIR [REPOSITORY:TAG]}
mkdir -p "$out"
out=$(cd "$out" && pwd)

layer() {
  if [[ ! -s "$out/wheel.whl" ]]; then
    curl --fail --silent --show-error --location --output "$out/wheel.whl" "$WHEEL_URL"
  fi
  rm -rf "$out/bin"
  mkdir -p "$out/bin"
  if [[ -n "${COMPILE_CMD:-}" ]]; then
    $COMPILE_CMD "$out/bin"
  else
    docker run --rm --user "$(id -u):$(id -g)" \
      -v "$here:/src:ro" -v "$out/bin:/out" "$BUILDER_IMAGE" /src/compile.sh /out
  fi
  python3 -I "$here/make-layer.py" "$out/wheel.whl" "$out/bin" "$out/layer.tar"
}

# One crane call appends the layer, sets the user and the labels. The base keeps
# its user 65532 and its environment; the template gives the command.
mutate() {
  crane mutate --platform linux/amd64 --append "$out/layer.tar" \
    --user 65532:65532 --workdir / \
    --label "org.opencontainers.image.source=https://github.com/jomcgi-org/homelab" \
    --label "org.opencontainers.image.description=Semgrep OSS engine ${SEMGREP_VERSION} with the listen-after-fork server for the gVisor sandbox fork tests" \
    --label "org.opencontainers.image.licenses=LGPL-2.1-or-later" \
    --label "org.opencontainers.image.base.name=${BASE_IMAGE}" \
    "$@" "$BASE_IMAGE"
}

case "$cmd" in
  layer) layer ;;
  image) mutate --output "$out/image.tar" --tag "engine-oss:local" ;;
  push) mutate --tag "${3:?usage: build.sh push OUT_DIR REPOSITORY:TAG}" ;;
  *) echo "unknown command $cmd" >&2; exit 64 ;;
esac
