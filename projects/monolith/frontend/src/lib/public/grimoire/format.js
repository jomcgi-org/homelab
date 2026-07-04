// Presentation helpers shared by the public entity renderers. Framework-free,
// ported from the private lib/grimoire/format.js verbatim (same JSONB shapes
// come back from the public /entities/{id} endpoint, just without any
// grant/recognition fields) so the two design systems can restyle
// independently without sharing an import across the private/public split.

// Fields that are spine/meta/projection plumbing, never rendered as content.
export const META_FIELDS = new Set([
  "id",
  "entity_type",
  "name",
  "source_type",
  "is_global",
  "source_book",
  "created_in_session",
  "created_at",
  "kind",
  "score",
  "mention_text",
]);

export function formatFieldName(key) {
  return String(key).replaceAll("_", " ");
}

// D&D ability modifier: floor((score - 10) / 2), signed.
export function abilityModifier(score) {
  const n = Number(score);
  if (!Number.isFinite(n)) return "";
  const mod = Math.floor((n - 10) / 2);
  return mod >= 0 ? `+${mod}` : `${mod}`;
}

export const ABILITIES = ["str", "dex", "con", "int", "wis", "cha"];

// {"walk": 40, "fly": 80} -> "walk 40 ft., fly 80 ft."; a plain string passes
// through; anything else becomes "".
export function formatSpeed(speed) {
  if (!speed) return "";
  if (typeof speed === "string") return speed;
  if (typeof speed === "object") {
    const parts = Object.entries(speed).map(([mode, dist]) =>
      typeof dist === "number" ? `${mode} ${dist} ft.` : `${mode} ${dist}`,
    );
    return parts.join(", ");
  }
  return "";
}

// Normalize a JSONB actions/traits payload into [{name, text}] blocks. Accepts a
// list of objects ({name, desc|description|text|...}), a list of strings, or a
// {name: text} dict, so the renderer never has to know which shape extraction
// produced and never falls back to raw JSON.
export function normalizeBlocks(value) {
  if (!value) return [];
  const blocks = [];
  if (Array.isArray(value)) {
    for (const item of value) {
      if (item == null) continue;
      if (typeof item === "string") {
        blocks.push({ name: "", text: item });
      } else if (typeof item === "object") {
        const name = item.name ?? item.title ?? "";
        const text =
          item.desc ??
          item.description ??
          item.text ??
          item.effect ??
          objectToText(item, ["name", "title"]);
        blocks.push({ name, text });
      }
    }
  } else if (typeof value === "object") {
    for (const [name, text] of Object.entries(value)) {
      blocks.push({ name, text: scalarToText(text) });
    }
  } else if (typeof value === "string") {
    blocks.push({ name: "", text: value });
  }
  return blocks.filter((b) => b.name || b.text);
}

function scalarToText(v) {
  if (v == null) return "";
  if (
    typeof v === "string" ||
    typeof v === "number" ||
    typeof v === "boolean"
  ) {
    return String(v);
  }
  if (Array.isArray(v)) return v.map(scalarToText).filter(Boolean).join(", ");
  if (typeof v === "object") return objectToText(v);
  return "";
}

// Render an object as "key value" prose (never JSON), skipping the given keys.
function objectToText(obj, skip = []) {
  return Object.entries(obj)
    .filter(([k]) => !skip.includes(k))
    .map(([k, v]) => `${formatFieldName(k)}: ${scalarToText(v)}`)
    .filter((s) => !s.endsWith(": "))
    .join("; ");
}

// A field is "long prose" (render as a paragraph) vs a short inline value.
export function isProse(key, value) {
  return (
    typeof value === "string" && (value.length > 80 || key === "description")
  );
}

export { scalarToText };
