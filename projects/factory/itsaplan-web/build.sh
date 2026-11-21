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
patch --batch --forward -p1 --fuzz=0 <"$patch_file"
# Hash the patched source and dependency lock independently of the installer
# archive's timestamps and filesystem order.
build_id=$(tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
	--mode='u+rw,go+r,go-w' --exclude=node_modules -cf - . | sha256sum)
export ITSAPLAN_FACTORY_BUILD_ID="${build_id%% *}"
# Next embeds a key even when the app has no Server Actions. This value is
# public. Reject the build if upstream introduces an action that needs a key.
export NEXT_SERVER_ACTIONS_ENCRYPTION_KEY
NEXT_SERVER_ACTIONS_ENCRYPTION_KEY=$("$node" -p 'Buffer.from(process.env.ITSAPLAN_FACTORY_BUILD_ID, "hex").toString("base64")')
cd apps/web
export NEXT_TELEMETRY_DISABLED=1
# SWC checks cache ancestors. Bazel may map the sandbox's sticky temporary
# directory owner to an unmapped UID. Use the private action directory.
export SWC_NATIVE_BINDING_CACHE="$work/swc-cache"
export SWC_NATIVE_BINDING_CACHE_SKIP_SECURITY_CHECK=1
export PATH="$(dirname "$node"):$PATH"
"$bun" test --preload ./test/setup.ts src/features/runner-queue/RunnerQueueRunPage.test.tsx
"$node" node_modules/next/dist/bin/next build
"$node" -e '
  const {node, edge} = require("./.next/server/server-reference-manifest.json");
  if (Object.keys(node).length || Object.keys(edge).length) {
    throw new Error("Server Actions require a private build key");
  }
'
cp -a .next/standalone "$work/image/app"
cp -a .next/static "$work/image/app/apps/web/.next/static"
cp -a public "$work/image/app/apps/web/public"
# This app has no Draft Mode routes. A non-string, falsey ID cannot match
# a cookie or revalidation header, and keeps Next's preview API disabled.
"$node" - "$work/image/app/apps/web" "$work/source" <<'NODE'
const fs = require("node:fs");
const path = require("node:path");
const [web, source] = process.argv.slice(2);
function rewrite(file, transform) {
  const name = path.join(web, file);
  fs.writeFileSync(name, transform(fs.readFileSync(name, "utf8")));
}
const noPreview = {
  previewModeId: 0,
  previewModeSigningKey: null,
  previewModeEncryptionKey: null,
};
rewrite(".next/prerender-manifest.json", (text) => {
  const manifest = JSON.parse(text);
  manifest.preview = noPreview;
  return JSON.stringify(manifest);
});
rewrite(".next/server/preview-props.json", () => JSON.stringify(noPreview));
// Next embeds its build checkout in the standalone server config. Use the
// image's actual source root in those files.
for (const file of ["server.js", ".next/required-server-files.json"]) {
  rewrite(file, (text) => text.replaceAll(source, "/app"));
}
NODE
tar -C "$work/image" --sort=name --mtime=@0 --owner=65532 --group=65532 -cf "$output" app
