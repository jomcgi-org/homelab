#!/usr/bin/env bash
# Provision the Ubuntu BuildBuddy runner, then run the verified wrapper.
set -euo pipefail
repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
runner_cache="${XDG_CACHE_HOME:-$HOME/.cache}/grimoire-browser-ci"
mkdir -p "$runner_cache"
root_command=()
if [[ "$(id -u)" -ne 0 ]]; then
	root_command=(sudo -n)
fi
# Chromium resolves localhost subdomains itself, but Playwright API requests
# use Node's system resolver. Keep both on the same synthetic loopback host.
if ! grep -Eq '^127\.0\.0\.1[[:space:]]+friends\.localhost([[:space:]]|$)' /etc/hosts; then
	printf '127.0.0.1 friends.localhost\n' | "${root_command[@]}" tee -a /etc/hosts >/dev/null
fi
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--dns-result-order=ipv4first"
"${root_command[@]}" apt-get update -qq
"${root_command[@]}" apt-get install -y -qq postgresql-16 postgresql-16-pgvector python3-venv

# Use the repository's tools image for the same Node and pnpm as local work.
tools_image="$(head -n 1 .tools-version)"
docker pull "$tools_image"
image_id="$(docker image inspect --format '{{.Id}}' "$tools_image")"
if [[ ! -f "$runner_cache/tools-image-id" ]] || [[ "$(cat "$runner_cache/tools-image-id")" != "$image_id" ]]; then
	tools_container="$(docker create "$tools_image" /bin/true)"
	trap 'docker rm "$tools_container" >/dev/null' EXIT
	mkdir -p "$runner_cache/tools/usr"
	docker cp "$tools_container:/usr/." "$runner_cache/tools/usr/"
	docker rm "$tools_container" >/dev/null
	trap - EXIT
	printf '%s\n' "$image_id" >"$runner_cache/tools-image-id"
fi
export PATH="$runner_cache/tools/usr/bin:$PATH"
if [[ ! -x "$runner_cache/bootstrap/bin/uv" ]]; then
	python3 -m venv "$runner_cache/bootstrap"
	"$runner_cache/bootstrap/bin/pip" install uv==0.12.7
fi
export PATH="$runner_cache/bootstrap/bin:$PATH"
uv venv --python 3.13 "$runner_cache/browser-installer"
uv pip install --python "$runner_cache/browser-installer/bin/python" playwright==1.63.0
"${root_command[@]}" "$runner_cache/browser-installer/bin/playwright" install-deps chromium
export GRIMOIRE_EVIDENCE_DIR="${BUILDBUDDY_ARTIFACTS_DIRECTORY:?BuildBuddy artifacts directory is required}/grimoire"
exec projects/monolith/dev/grimoire/ci.sh
