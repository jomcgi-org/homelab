import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const address = (bytes) =>
  `${createHash("sha256").update(bytes).digest("hex")}.json`;
const original = ['{"original":"A"}\n', '{"original":"B"}\n']
  .map((bytes) => ({ name: address(bytes), bytes }))
  .sort((a, b) => a.name.localeCompare(b.name));

async function fixture(t, objects) {
  const root = await mkdtemp(join(tmpdir(), "storyboard-rehash-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, "objects"));
  await copyFile(
    new URL("./rehash.mjs", import.meta.url),
    join(root, "rehash.mjs"),
  );
  for (const [name, bytes] of Object.entries(objects)) {
    await writeFile(join(root, "objects", name), bytes);
  }
  const [story, goal = story] = Object.keys(objects).map((n) => `objects/${n}`);
  const manifest = `${JSON.stringify({ story, goals: { demo: goal } }, null, 2)}\n`;
  await writeFile(join(root, "manifest.json"), manifest);
  return { root, manifest };
}

async function snapshot(root) {
  const paths = [
    "manifest.json",
    ...(await readdir(join(root, "objects"))).sort().map((n) => `objects/${n}`),
  ];
  return Object.fromEntries(
    await Promise.all(paths.map(async (p) => [p, await readFile(join(root, p))])),
  );
}

function run(root) {
  return spawnSync(process.execPath, [join(root, "rehash.mjs")], {
    encoding: "utf8",
    timeout: 10_000,
  });
}

for (const swap of [false, true]) {
  test(`rejects an occupied destination, swapped contents: ${swap}`, async (t) => {
    const [a, b] = original;
    const { root } = await fixture(t, {
      // A would overwrite B before the old loop read B's independent edit.
      [a.name]: b.bytes,
      [b.name]: swap ? a.bytes : '{"independent":"edit to B"}\n',
      // This sorts first: preflight must also prevent an unrelated rename.
      [`${"0".repeat(64)}.json`]: '{"unrelated":"edit"}\n',
    });
    const before = await snapshot(root);
    const result = run(root);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Rehash conflict/);
    assert.deepEqual(await snapshot(root), before);
  });
}

test("rejects duplicate destinations without modifying any file", async (t) => {
  const { root } = await fixture(t, {
    [original[0].name]: '{"same":"new contents"}\n',
    [original[1].name]: '{"same":"new contents"}\n',
  });
  const before = await snapshot(root);
  const result = run(root);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Rehash conflict/);
  assert.deepEqual(await snapshot(root), before);
});

test("rehashes exact bytes, updates references and is repeatable", async (t) => {
  const edits = ['{ "edited": "A" }\n', '{"edited":"B"}\n\n'];
  const { root, manifest } = await fixture(t, {
    [original[0].name]: edits[0],
    [original[1].name]: edits[1],
  });
  const result = run(root);
  assert.equal(result.status, 0, result.stderr);
  const after = await snapshot(root);
  const paths = edits.map((bytes) => `objects/${address(bytes)}`);
  const expected = manifest
    .replace(`objects/${original[0].name}`, paths[0])
    .replace(`objects/${original[1].name}`, paths[1]);
  assert.equal(after["manifest.json"].toString(), expected);
  assert.deepEqual(Object.keys(after).sort(), ["manifest.json", ...paths].sort());
  for (const [i, path] of paths.entries()) {
    assert.equal(after[path].toString(), edits[i]);
  }
  const repeated = run(root);
  assert.equal(repeated.status, 0, repeated.stderr);
  assert.deepEqual(await snapshot(root), after);
});
