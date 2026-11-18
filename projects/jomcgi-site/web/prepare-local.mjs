import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { prepareDocs } from "./prepare-docs.mjs";

execFileSync(
  "python3",
  [
    "projects/monolith/knowledge/tools/gen_docs_manifest.py",
    "--out",
    "projects/jomcgi-site/web/docs-manifest.json",
  ],
  {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    stdio: "inherit",
  },
);
await prepareDocs();
