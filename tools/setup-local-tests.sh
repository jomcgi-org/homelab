#!/usr/bin/env bash
# Local language-tool tests only. This does not modify ci or remote/network settings.
set -euo pipefail
repo="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [[ "$(uname -s)/$(uname -m)" != "Linux/x86_64" ]]; then
	echo "Local test setup supports Linux x86_64 only." >&2
	exit 1
fi
for command in python3 curl uv; do
	command -v "$command" >/dev/null || {
		echo "Missing prerequisite: $command" >&2
		exit 1
	}
done
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"'
state=/workspace/.cloud/local-tests
mkdir -p "$state" /workspace/.cloud/cache/go-build /workspace/.cloud/go
export GOPATH=/workspace/.cloud/go
export GOCACHE=/workspace/.cloud/cache/go-build
export GOBIN="$state/bin"
export GOTOOLCHAIN=local
export UV_CACHE_DIR=/workspace/.cloud/cache/uv
mkdir -p "$GOBIN"
python3 - "$repo" "$state" <<'PY'
import hashlib, json, pathlib, re, subprocess, sys, tarfile, zipfile
repo, state = map(pathlib.Path, sys.argv[1:])
version = 'go' + re.search(r'^go (\S+)', (repo/'go.mod').read_text(), re.M)[1]
def download(url, path):
    subprocess.run(['curl', '--fail', '--show-error', '--silent', '--location', url, '-o', str(path)], check=True)
if not (state/version/'go/bin/go').is_file():
    manifest = state/'go-releases.json'
    download('https://go.dev/dl/?mode=json&include=all', manifest)
    release = next(r for r in json.loads(manifest.read_text()) if r['version'] == version)
    archive = next(f for f in release['files'] if f['os']=='linux' and f['arch']=='amd64' and f['kind']=='archive')
    path = state/archive['filename']
    download('https://go.dev/dl/'+archive['filename'], path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == archive['sha256'], 'Go archive checksum mismatch'
    (state/version).mkdir(exist_ok=True)
    with tarfile.open(path) as source:
        source.extractall(state/version, filter='data')
config = (repo/'bazel/erlang/repositories.bzl').read_text()
block = config[config.index('name = "protoc_linux_x86_64"'):]
url = re.search(r'urls = \["([^"]+)"\]', block)[1]
checksum = re.search(r'sha256 = "([0-9a-f]+)"', block)[1]
protoc_dir = state/('protoc-'+checksum[:12])
if not (protoc_dir/'bin/protoc').is_file():
    archive = state/'protoc.zip'
    download(url, archive)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == checksum, 'protoc archive checksum mismatch'
    with zipfile.ZipFile(archive) as source:
        source.extractall(protoc_dir)
    (protoc_dir/'bin/protoc').chmod(0o755)
(state/'env.sh').write_text(
    f'export PATH="{state/version}/go/bin:{protoc_dir}/bin:{state}/bin:$PATH"\n'
    'export GOPATH=/workspace/.cloud/go\n'
    'export GOCACHE=/workspace/.cloud/cache/go-build\n'
    f'export GOBIN="{state}/bin"\n'
    'export GOTOOLCHAIN=local\n'
)
# Plain version constraints for a small local test venv, derived from repo lock.
constraints=[]
for line in (repo/'bazel/requirements/all.txt').read_text().splitlines():
    match=re.match(r'^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^ ;\\]+)', line)
    if match: constraints.append(f'{match[1]}=={match[2]}')
(state/'constraints.txt').write_text('\n'.join(dict.fromkeys(constraints))+'\n')
PY
# Generated above; no source file exists in the checkout.
# shellcheck source=/dev/null
source "$state/env.sh"
cd "$repo"
protobuf_version=$(awk '$1 == "google.golang.org/protobuf" { print $2 }' go.mod)
grpc_plugin_version=$(awk '$1 == "google.golang.org/grpc/cmd/protoc-gen-go-grpc" { print $2 }' go.mod)
go install "google.golang.org/protobuf/cmd/protoc-gen-go@$protobuf_version"
go install "google.golang.org/grpc/cmd/protoc-gen-go-grpc@$grpc_plugin_version"
if [[ ! -x "$state/venv/bin/python" ]]; then
	uv venv "$state/venv"
fi
uv pip install --python "$state/venv/bin/python" --constraint "$state/constraints.txt" \
	pytest pytest-asyncio sqlmodel PyYAML httpx jsonschema kubernetes-asyncio pgvector \
	opentelemetry-api genai-prices dbos fastapi
printf 'Local tools ready. Source %s/env.sh; Python: %s/venv/bin/python\n' "$state" "$state"
go version
protoc --version
