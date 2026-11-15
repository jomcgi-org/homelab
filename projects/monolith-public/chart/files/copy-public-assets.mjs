import { lstat, mkdir, readdir, realpath, copyFile } from "node:fs/promises";
import { constants } from "node:fs";
import { resolve, join } from "node:path";

// The Job supplies the immutable directory from its exact public image digest.
// Never copy the parent client directory, server code, or linked files.
const source = resolve(process.argv[1]);
const destination = resolve(process.argv[2]);
if (
  (await realpath(source)) !== source ||
  !(await lstat(source)).isDirectory()
) {
  throw new Error("Public immutable input must be a real directory");
}
const files = [];
let totalBytes = 0;
async function inspect(directory, relative = "") {
  for (const name of (await readdir(directory)).sort()) {
    if (!/^[A-Za-z0-9_.-]+$/.test(name) || name === "." || name === "..") {
      throw new Error("Invalid public asset path");
    }
    const path = join(directory, name);
    const key = relative ? `${relative}/${name}` : name;
    const info = await lstat(path);
    if (info.isSymbolicLink()) throw new Error("Linked public asset rejected");
    if (info.isDirectory()) {
      await inspect(path, key);
    } else if (info.isFile()) {
      totalBytes += info.size;
      files.push({ path, key });
      if (
        info.size > 16 * 1024 * 1024 ||
        totalBytes > 64 * 1024 * 1024 ||
        files.length > 4096
      ) {
        throw new Error("Public asset copy exceeds its bounds");
      }
    } else {
      throw new Error("Non-file public asset rejected");
    }
  }
}
await inspect(source);
if (!files.length) throw new Error("Public immutable directory is empty");
// Validate the whole source before copying anything. The image root is read-only.
for (const { path, key } of files) {
  const output = join(destination, key);
  await mkdir(resolve(output, ".."), { recursive: true });
  await copyFile(path, output, constants.COPYFILE_EXCL);
}
console.log(
  `Copied ${files.length} public immutable files (${totalBytes} bytes)`,
);
