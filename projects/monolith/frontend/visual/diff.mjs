import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";

const HERE = dirname(fileURLToPath(import.meta.url));
const FLOOR = 50,
  RATIO = 0.001;

export function isChanged({ mismatched = 0, total = 1, added = false }) {
  if (added) return true;
  return mismatched > FLOOR && mismatched / total > RATIO;
}

// Resolve an execroot-relative path (set by the BUILD) to absolute: this
// js_run_binary runs from the execroot with cwd under <execroot>/bazel-out/...,
// so a bare relative path would not resolve. See capture.mjs for the same trap.
function fromExecroot(rel) {
  const cwd = process.cwd();
  return cwd.includes("/bazel-out/")
    ? resolve(cwd.split("/bazel-out/")[0], rel)
    : rel;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  // Inputs/outputs under Bazel: the capture PNGs are a separate target's output
  // (CAPTURE_OUT, execroot-relative); baselines are committed srcs read via HERE
  // (read-only runfiles); the report + diff PNGs are written to the declared
  // out_dir (BAZEL_BINDIR + DIFF_OUT_SUBDIR). All fall back to HERE for a local
  // smoke run.
  const captureOut = process.env.CAPTURE_OUT
    ? fromExecroot(process.env.CAPTURE_OUT)
    : join(HERE, "out");
  const outDir =
    process.env.BAZEL_BINDIR && process.env.DIFF_OUT_SUBDIR
      ? fromExecroot(
          join(process.env.BAZEL_BINDIR, process.env.DIFF_OUT_SUBDIR),
        )
      : join(HERE, "out");
  const baselineDir = join(HERE, "baseline");

  const names = JSON.parse(readFileSync(join(captureOut, "manifest.json")));
  mkdirSync(join(outDir, "diff"), { recursive: true });
  const report = { changed: [], added: [], unchanged: 0 };
  for (const name of names) {
    const curPath = join(captureOut, `${name}.png`);
    const basePath = join(baselineDir, `${name}.png`);
    if (!existsSync(basePath)) {
      report.added.push(name);
      continue;
    }
    const cur = PNG.sync.read(readFileSync(curPath));
    const base = PNG.sync.read(readFileSync(basePath));
    if (cur.width !== base.width || cur.height !== base.height) {
      report.changed.push(name);
      continue;
    }
    const diff = new PNG({ width: cur.width, height: cur.height });
    const mismatched = pixelmatch(
      base.data,
      cur.data,
      diff.data,
      cur.width,
      cur.height,
      {
        threshold: 0.1,
      },
    );
    if (isChanged({ mismatched, total: cur.width * cur.height })) {
      writeFileSync(join(outDir, `diff/${name}.png`), PNG.sync.write(diff));
      report.changed.push(name);
    } else report.unchanged++;
  }
  writeFileSync(join(outDir, "report.json"), JSON.stringify(report, null, 2));
  console.log(
    `changed=${report.changed.length} added=${report.added.length} unchanged=${report.unchanged}`,
  );
}
