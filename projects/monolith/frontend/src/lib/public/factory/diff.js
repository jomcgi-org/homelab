/**
 * A unified diff as files, hunks and numbered lines. The diff is worker
 * output and is parsed into rows rather than marked up, so nothing in it can
 * reach the page as HTML. Tolerant by design: a clipped diff, a bare hunk
 * with no file header, or a binary stub all still come back as something the
 * view can draw.
 */

const FILE_HEADER = /^diff --git a\/(.+?) b\/(.+)$/;
const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

function newFile(path, oldPath) {
  return {
    path,
    oldPath,
    kind: "modified",
    additions: 0,
    deletions: 0,
    hunks: [],
  };
}

/**
 * @param {string | null | undefined} diff
 * @returns {{ files: Array<{path: string, oldPath: string, kind: string,
 *   additions: number, deletions: number,
 *   hunks: Array<{header: string, lines: Array<{kind: string, old: number | null, new: number | null, text: string}>}>}>,
 *   additions: number, deletions: number }}
 */
export function parseDiff(diff) {
  const files = [];
  if (!diff) return { files, additions: 0, deletions: 0 };
  let file = null;
  let hunk = null;
  let oldNo = 0;
  let newNo = 0;
  for (const line of diff.split("\n")) {
    const header = FILE_HEADER.exec(line);
    if (header) {
      file = newFile(header[2], header[1]);
      files.push(file);
      hunk = null;
      continue;
    }
    if (line.startsWith("Binary files")) {
      if (file) file.kind = "binary";
      continue;
    }
    if (line.startsWith("new file mode")) {
      if (file) file.kind = "added";
      continue;
    }
    if (line.startsWith("deleted file mode")) {
      if (file) file.kind = "deleted";
      continue;
    }
    if (line.startsWith("rename from") || line.startsWith("rename to")) {
      if (file) file.kind = "renamed";
      continue;
    }
    if (line.startsWith("--- ") || line.startsWith("+++ ")) {
      // A bare hunk with no `diff --git` line still names its file here.
      if (!file && line.startsWith("+++ ")) {
        file = newFile(line.slice(4).replace(/^b\//, ""), "");
        files.push(file);
      }
      if (file && line === "+++ /dev/null") file.kind = "deleted";
      if (file && line === "--- /dev/null") file.kind = "added";
      continue;
    }
    const at = HUNK_HEADER.exec(line);
    if (at) {
      if (!file) {
        file = newFile("", "");
        files.push(file);
      }
      oldNo = Number(at[1]);
      newNo = Number(at[3]);
      hunk = { header: line, lines: [] };
      file.hunks.push(hunk);
      continue;
    }
    if (!hunk) continue;
    if (line.startsWith("\\")) {
      hunk.lines.push({ kind: "note", old: null, new: null, text: line });
      continue;
    }
    if (line.startsWith("+")) {
      hunk.lines.push({
        kind: "add",
        old: null,
        new: newNo++,
        text: line.slice(1),
      });
      file.additions += 1;
    } else if (line.startsWith("-")) {
      hunk.lines.push({
        kind: "del",
        old: oldNo++,
        new: null,
        text: line.slice(1),
      });
      file.deletions += 1;
    } else {
      // A context line keeps its leading space in the diff; the row drops it.
      // The final empty string after a trailing newline is not a line.
      hunk.lines.push({
        kind: "ctx",
        old: oldNo++,
        new: newNo++,
        text: line.startsWith(" ") ? line.slice(1) : line,
      });
    }
  }
  for (const entry of files) {
    const last = entry.hunks.at(-1);
    const tail = last?.lines.at(-1);
    if (tail && tail.kind === "ctx" && tail.text === "" && last.lines.length) {
      last.lines.pop();
    }
  }
  return {
    files,
    additions: files.reduce((sum, entry) => sum + entry.additions, 0),
    deletions: files.reduce((sum, entry) => sum + entry.deletions, 0),
  };
}

/**
 * The file in a parsed diff that an activity's path points at. The shim
 * records the path the worker used, often absolute inside its workspace, and
 * the diff names repo-relative paths, so the match is on the suffix.
 */
export function fileFor(parsed, path) {
  if (!parsed?.files?.length || !path) return null;
  const wanted = String(path).replace(/^\.\//, "");
  return (
    parsed.files.find((entry) => entry.path === wanted) ??
    parsed.files.find(
      (entry) =>
        entry.path &&
        (wanted.endsWith(`/${entry.path}`) ||
          entry.path.endsWith(`/${wanted}`)),
    ) ??
    null
  );
}

/** The directory and the file name, so the name can carry the weight. */
export function splitPath(path) {
  const text = path ?? "";
  const cut = text.lastIndexOf("/");
  return cut < 0
    ? { dir: "", name: text }
    : { dir: text.slice(0, cut + 1), name: text.slice(cut + 1) };
}
