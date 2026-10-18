#!/usr/bin/env bash
set -euo pipefail

if [ -n "${TEST_SRCDIR:-}" ]; then
	script="$TEST_SRCDIR/$TEST_WORKSPACE/projects/embervm/proto/embervm/node/v1/sync-go-stubs.sh"
else
	script=$(realpath "$(dirname "$0")/sync-go-stubs.sh")
fi
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
cd "$scratch"
printf 'message\n' >message.go
printf 'service\n' >service.go
cp message.go tracked-message.go
cp service.go tracked-service.go

bash "$script" --check message.go service.go tracked-message.go tracked-service.go
ln -s missing-target.go dangling.go
if out=$(bash "$script" --check dangling.go service.go tracked-message.go tracked-service.go 2>&1); then
	echo "a dangling generated stub was accepted" >&2
	exit 1
fi
case "$out" in *"is missing"*) ;; *) echo "a dangling generated stub failed silently: $out" >&2; exit 1 ;; esac
printf 'stale\n' >tracked-message.go
if bash "$script" --check message.go service.go tracked-message.go tracked-service.go; then
	echo "message drift was accepted" >&2
	exit 1
fi
cp message.go tracked-message.go
printf 'stale\n' >tracked-service.go
if bash "$script" --check message.go service.go tracked-message.go tracked-service.go; then
	echo "service drift was accepted" >&2
	exit 1
fi

mkdir repo
git -C repo init -q
package=projects/embervm/proto/embervm/node/v1
mkdir -p "repo/$package"
printf 'original message\n' >"repo/$package/node.pb.go"
printf 'original service\n' >"repo/$package/node_grpc.pb.go"
cp "repo/$package/node.pb.go" original-message.go
git -C repo add "$package/node.pb.go"
if BUILD_WORKSPACE_DIRECTORY="$scratch/repo" bash "$script" --update message.go service.go; then
	echo "untracked service stub was accepted for writeback" >&2
	exit 1
fi
cmp original-message.go "repo/$package/node.pb.go"
git -C repo add "$package/node_grpc.pb.go"
BUILD_WORKSPACE_DIRECTORY="$scratch/repo" bash "$script" --update message.go service.go
bash "$script" --check message.go service.go "repo/$package/node.pb.go" "repo/$package/node_grpc.pb.go"
# A repeat update is byte-stable, and no other paths were created or tracked.
BUILD_WORKSPACE_DIRECTORY="$scratch/repo" bash "$script" --update message.go service.go
test "$(git -C repo ls-files | wc -l)" -eq 2
bash "$script" --check message.go service.go "repo/$package/node.pb.go" "repo/$package/node_grpc.pb.go"
if bash "$script" --unknown; then
	echo "invalid mode was accepted" >&2
	exit 1
fi
