const metadata = new Set([
  "id",
  "entity_type",
  "name",
  "source_type",
  "is_global",
  "source_book",
  "created_in_session",
  "created_at",
  "grants",
  "grant",
  "recognition_only",
]);

export function knowledgeFields(entity) {
  const details = entity?.revealed_details || entity || {};
  return Object.entries(details).filter(
    ([key, value]) =>
      !metadata.has(key) && value !== null && value !== undefined,
  );
}

export function selectedDetails(entity, keys, clue = "") {
  const allowed = Object.fromEntries(knowledgeFields(entity));
  const result = Object.fromEntries(
    keys
      .filter((key) => Object.hasOwn(allowed, key))
      .map((key) => [key, allowed[key]]),
  );
  if (clue.trim()) result.clue = clue.trim();
  return result;
}
