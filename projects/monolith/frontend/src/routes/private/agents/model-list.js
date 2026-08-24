// The offered model catalogue (issue #4859). GET /api/agents/models serves
// SUPPORTED_MODELS narrowed by the AGENT_MODELS env var, and the console has
// NO bundled fallback list: whatever the endpoint returns is what the picker
// offers, so an empty or misconfigured catalogue renders visibly empty
// instead of silently resurrecting a hardcoded selection.

// Extract the raw catalogue entries from a response body. Anything that is
// not an array, or entries without a usable name, are dropped rather than
// defaulted: there is no list to fall back to.
export function modelEntries(body) {
  const models = body?.models;
  if (!Array.isArray(models)) return [];
  return models.filter(
    (entry) =>
      entry != null &&
      (typeof entry === "string" || typeof entry.name === "string"),
  );
}

export function modelName(entry) {
  return typeof entry === "string" ? entry : entry.name;
}

// Display hint: the pi family runs in-cluster on the free llama.cpp lane, so
// the picker labels it "(local)". Every other family renders as the bare
// name; the endpoint carries no presentation beyond family.
export function modelLabel(entry) {
  const name = modelName(entry);
  if (!name) return "";
  return entry.family === "pi" ? `${name} (local)` : name;
}
