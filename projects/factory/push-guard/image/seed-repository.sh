#!/bin/sh
set -eu

repo=/var/lib/factory-push-guard/homelab.git
if [ ! -d "$repo" ]; then
	staging=$(mktemp -d /var/lib/factory-push-guard/seed.XXXXXX)
	trap 'rm -rf "$staging"' EXIT
	git clone --bare --no-tags https://github.com/jomcgi-org/homelab.git "$staging"
	mv -T "$staging" "$repo"
fi
test "$(git --git-dir="$repo" rev-parse --is-bare-repository)" = true
git --git-dir="$repo" config core.hooksPath /usr/local/libexec/factory-push-guard
git --git-dir="$repo" config receive.fsckObjects true

# The operator uploads the digest-named index to this private volume before
# the receiver can start. Copy to a temporary name, then rename atomically.
mkdir -p /var/lib/factory-push-guard/indices
index="/var/lib/factory-push-guard/indices/$PUSH_GUARD_INDEX_SHA256.json"
until [ -s "$index" ]; do
	sleep 5
done
