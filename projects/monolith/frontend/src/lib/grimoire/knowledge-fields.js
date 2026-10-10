// Mirrors _SPINE_FIELDS in projects/monolith/grimoire/visibility.py: the
// identity and bookkeeping columns every projection carries, never shown as
// knowledge. fixtures/reveal-projections.json pins both sides together.
export const SPINE_FIELDS = [
  "id",
  "entity_type",
  "name",
  "source_type",
  "is_global",
  "source_book",
  "created_in_session",
  "created_at",
];

// Grant annotations the server adds beside the spine (DM view and stubs).
const ANNOTATION_FIELDS = ["grants", "grant", "recognition_only"];

const metadata = new Set([...SPINE_FIELDS, ...ANNOTATION_FIELDS]);

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

// Normalises a reveal event entry or a preview/lookup projection into the one
// shape RevealProjection renders. A name-only entry carries no entity, so the
// identity comes from the event fields.
export function projectionOf(knowledge) {
  const entity = knowledge?.projection || knowledge?.entity || {};
  return {
    ...entity,
    id: entity.id ?? knowledge?.entity_id,
    name: entity.name ?? knowledge?.name,
    entity_type: entity.entity_type ?? knowledge?.entity_type,
  };
}

export function scopeOf(knowledge) {
  if (knowledge?.grant_scope) return knowledge.grant_scope;
  const projection = knowledge?.projection || knowledge?.entity || knowledge;
  if (projection?.recognition_only) return "name_only";
  return projection?.revealed_details ? "partial" : "full";
}
