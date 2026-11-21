#!/usr/bin/env bash
set -euo pipefail

archive=$(realpath "$1")
patch_file=$(realpath "$2")
node=$(realpath "$3")
bun=$(realpath "$4")
output=$(realpath -m "$5")
work=$(mktemp -d "$(dirname "$output")/itsaplan-web.XXXXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir "$work/source" "$work/image"
tar -xf "$archive" -C "$work/source"
cd "$work/source"
patch --batch --forward -p1 --fuzz=0 < "$patch_file"
cd apps/web
export NEXT_TELEMETRY_DISABLED=1
# SWC checks cache ancestors. Bazel may map the sandbox's sticky temporary
# directory owner to an unmapped UID. Use the private action directory.
export SWC_NATIVE_BINDING_CACHE="$work/swc-cache"
export SWC_NATIVE_BINDING_CACHE_SKIP_SECURITY_CHECK=1
export PATH="$(dirname "$node"):$PATH"
"$bun" test --preload ./test/setup.ts src/features/runner-queue/RunnerQueueRunPage.test.tsx
"$node" node_modules/next/dist/bin/next build
cp -a .next/standalone "$work/image/app"
cp -a .next/static "$work/image/app/apps/web/.next/static"
cp -a public "$work/image/app/apps/web/public"
tar -C "$work/image" --sort=name --mtime=@0 --owner=65532 --group=65532 -cf "$output" app
