import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";

const HERE = dirname(fileURLToPath(import.meta.url));
const FLOOR = 50,
  RATIO = 0.001;

export function isChanged({ mismatched = 0, total = 1, added = false }) {
  if (added) return true;
  return mismatched > FLOOR && mismatched / total > RATIO;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  // Branch-vs-main: there are no committed baselines any more. CI captures the
  // PR (CAPTURE_DIR) and main (BASELINE_DIR) into two staged directories and
  // passes both as absolute paths; the report + diff PNGs are written to OUT_DIR.
  // All fall back to HERE for a local smoke run. No execroot path-juggling: the
  // tool is invoked with absolute paths, so cwd is irrelevant.
  const captureOut = process.env.CAPTURE_DIR || join(HERE, "out");
  const baselineDir = process.env.BASELINE_DIR || join(HERE, "out", "baseline");
  const outDir = process.env.OUT_DIR || captureOut;

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
