export function decodeSearchIndex(index) {
  if (
    !Array.isArray(index?.states) ||
    !Array.isArray(index?.entities) ||
    !Array.isArray(index?.notes)
  ) {
    return [];
  }

  const decoded = [];
  for (const row of index.notes) {
    if (
      !Array.isArray(row) ||
      row.length < 4 ||
      typeof row[0] !== "string" ||
      typeof row[1] !== "string"
    ) {
      continue;
    }
    const verificationState = index.states[row[2]];
    if (typeof verificationState !== "string") continue;
    const entity = row[3] === -1 ? null : index.entities[row[3]];
    decoded.push({
      note_id: row[0],
      title: row[1],
      // Lowercased once here rather than per keystroke: ranking runs over the
      // whole corpus on every input event, so any per-note work done there is
      // multiplied by thousands.
      search: row[1].toLocaleLowerCase(),
      verification_state: verificationState,
      entity: typeof entity === "string" ? entity : null,
    });
  }
  return decoded;
}

function matchBand(title, query) {
  const first = title.indexOf(query);
  if (first < 0) return -1;
  if (first === 0) return 0;

  let index = first;
  while (index >= 0) {
    if (!/[a-z0-9]/i.test(title[index - 1])) return 1;
    index = title.indexOf(query, index + 1);
  }
  return 2;
}

/**
 * Rank already-decoded notes against a query.
 *
 * Takes the decoded array, not the raw index: decoding allocates an object per
 * note, so doing it inside the ranking would rebuild the whole corpus on every
 * keystroke and give back the latency the index exists to remove.
 */
export function rankIndexMatches(notes, query, limit = 20) {
  const needle = String(query ?? "")
    .trim()
    .toLocaleLowerCase();
  const cappedLimit = Math.max(0, Math.floor(Number(limit) || 0));
  if (!needle || cappedLimit === 0 || !Array.isArray(notes)) return [];

  const bands = [[], [], []];
  for (const note of notes) {
    const band = matchBand(
      note.search ?? note.title.toLocaleLowerCase(),
      needle,
    );
    if (band >= 0) bands[band].push(note);
  }
  return bands.flat().slice(0, cappedLimit);
}
