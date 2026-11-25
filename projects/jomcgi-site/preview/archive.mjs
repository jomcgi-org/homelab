// Reads and checks a PR preview archive (a gzip tar of the built site) before
// any of it touches disk. The uploader accepts static files only: a Worker
// script, routes file or Functions directory would run code on Cloudflare,
// so the archive is refused if it holds one.
import { gunzipSync } from "node:zlib";

export const LIMITS = Object.freeze({
  // The current build is about 140 files and 9 MiB.
  files: 2000,
  fileBytes: 10 * 1024 * 1024,
  totalBytes: 64 * 1024 * 1024,
});

// Static file types the site build emits. Anything else is refused.
export const EXTENSIONS = new Set([
  "avif",
  "css",
  "gif",
  "html",
  "ico",
  "jpeg",
  "jpg",
  "js",
  "json",
  "map",
  "png",
  "svg",
  "ttf",
  "txt",
  "webmanifest",
  "webp",
  "woff",
  "woff2",
  "xml",
]);

// Files the asset server reads as configuration. Only these two, only at
// the root.
const ROOT_CONFIG = new Set(["_headers", "_redirects"]);

export class ArchiveError extends Error {
  constructor(message) {
    super(message);
    this.name = "ArchiveError";
  }
}

const fail = (message) => {
  throw new ArchiveError(message);
};

function text(block, start, length) {
  const field = block.subarray(start, start + length);
  const end = field.indexOf(0);
  return field.subarray(0, end === -1 ? length : end).toString("utf8");
}

function octal(block, start, length) {
  const value = text(block, start, length).trim();
  if (!/^[0-7]*$/.test(value)) fail("the archive has a bad size field");
  return value === "" ? 0 : parseInt(value, 8);
}

// pax extended header records: "<length> <key>=<value>\n".
function paxPath(data) {
  let path;
  let offset = 0;
  while (offset < data.length) {
    const space = data.indexOf(0x20, offset);
    if (space === -1) fail("the archive has a bad pax header");
    const length = Number(data.subarray(offset, space).toString("ascii"));
    if (!Number.isSafeInteger(length) || length <= space - offset) {
      fail("the archive has a bad pax header");
    }
    const record = data.subarray(space + 1, offset + length - 1).toString();
    const equals = record.indexOf("=");
    if (record.slice(0, equals) === "path") path = record.slice(equals + 1);
    offset += length;
  }
  return path;
}

// Returns [{ path, type, data }] for each tar member. type is "file", "dir"
// or the raw tar type flag for anything else, so the check can name it.
export function readTar(tar) {
  const entries = [];
  let offset = 0;
  let longName;
  while (offset + 512 <= tar.length) {
    const block = tar.subarray(offset, offset + 512);
    if (block.every((byte) => byte === 0)) break;
    const size = octal(block, 124, 12);
    const flag = String.fromCharCode(block[156] || 0x30);
    const start = offset + 512;
    if (start + size > tar.length) fail("the archive is truncated");
    const data = tar.subarray(start, start + size);
    offset = start + Math.ceil(size / 512) * 512;
    if (flag === "x" || flag === "L") {
      longName = flag === "x" ? paxPath(data) : text(data, 0, data.length);
      continue;
    }
    if (flag === "g") continue;
    const prefix = text(block, 345, 155);
    const name = text(block, 0, 100);
    const path = longName ?? (prefix ? `${prefix}/${name}` : name);
    longName = undefined;
    const type = flag === "0" ? "file" : flag === "5" ? "dir" : flag;
    entries.push({ path, type, data });
  }
  return entries;
}

// Normalises one member path, or throws if it may not be published.
export function checkPath(raw) {
  const path = raw.replace(/^(\.\/)+/, "").replace(/\/$/, "");
  if (path === "" || path === ".") return "";
  if (path.startsWith("/") || path.includes("\\") || /[\0-\x1f]/.test(path)) {
    fail(`refused path ${JSON.stringify(raw)}`);
  }
  const parts = path.split("/");
  for (const part of parts) {
    // Hidden names cover .., .vite, .assetsignore and wrangler's own
    // .wrangler redirect directory.
    if (part === "" || part.startsWith(".")) {
      fail(`refused path ${JSON.stringify(raw)}: hidden or empty segment`);
    }
    if (part === "_worker.js" || part === "_routes.json") {
      fail(`refused ${path}: Worker code is not allowed in a preview`);
    }
  }
  if (parts[0] === "functions") {
    fail(`refused ${path}: Functions are not allowed in a preview`);
  }
  return path;
}

function checkFile(path) {
  const name = path.split("/").pop();
  if (ROOT_CONFIG.has(name)) {
    if (path !== name)
      fail(`refused ${path}: ${name} is only allowed at the root`);
    return;
  }
  const dot = name.lastIndexOf(".");
  const extension = dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
  if (!EXTENSIONS.has(extension)) {
    fail(
      `refused ${path}: .${extension || "(none)"} is not a static file type`,
    );
  }
}

// Gunzips and checks a preview archive. Returns a Map of path to bytes.
export function readPreview(gzip, limits = LIMITS) {
  let tar;
  try {
    tar = gunzipSync(gzip, {
      maxOutputLength: limits.totalBytes + 1024 * 1024,
    });
  } catch (error) {
    if (error instanceof RangeError) {
      fail(`the archive is larger than ${limits.totalBytes} bytes`);
    }
    fail("the body is not a gzip archive");
  }
  const files = new Map();
  let total = 0;
  for (const entry of readTar(tar)) {
    const path = checkPath(entry.path);
    if (entry.type === "dir") continue;
    if (entry.type !== "file") {
      fail(
        `refused ${entry.path}: only regular files and directories are allowed`,
      );
    }
    if (path === "") fail("refused a file with an empty name");
    checkFile(path);
    if (files.has(path)) fail(`refused ${path}: it appears twice`);
    if (entry.data.length > limits.fileBytes) {
      fail(`refused ${path}: larger than ${limits.fileBytes} bytes`);
    }
    total += entry.data.length;
    if (total > limits.totalBytes) {
      fail(`the archive is larger than ${limits.totalBytes} bytes`);
    }
    if (files.size + 1 > limits.files) {
      fail(`the archive has more than ${limits.files} files`);
    }
    files.set(path, entry.data);
  }
  if (!files.has("index.html")) fail("the archive has no index.html");
  const headers = files.get("_headers")?.toString("utf8") ?? "";
  if (/^\s*!\s*x-robots-tag/im.test(headers)) {
    fail("refused _headers: a preview may not detach X-Robots-Tag");
  }
  // Every preview response says noindex, whatever the PR's _headers says.
  files.set(
    "_headers",
    Buffer.from(
      `${headers.replace(/\n*$/, "\n")}\n/*\n  X-Robots-Tag: noindex\n`,
    ),
  );
  return files;
}
