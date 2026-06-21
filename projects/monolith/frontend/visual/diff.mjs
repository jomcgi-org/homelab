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
  const names = JSON.parse(readFileSync(join(HERE, "out/manifest.json")));
  mkdirSync(join(HERE, "out/diff"), { recursive: true });
  const report = { changed: [], added: [], unchanged: 0 };
  for (const name of names) {
    const curPath = join(HERE, `out/${name}.png`);
    const basePath = join(HERE, `baseline/${name}.png`);
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
      writeFileSync(join(HERE, `out/diff/${name}.png`), PNG.sync.write(diff));
      report.changed.push(name);
    } else report.unchanged++;
  }
  writeFileSync(join(HERE, "out/report.json"), JSON.stringify(report, null, 2));
  console.log(
    `changed=${report.changed.length} added=${report.added.length} unchanged=${report.unchanged}`,
  );
}
