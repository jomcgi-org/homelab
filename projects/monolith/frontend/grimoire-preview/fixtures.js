// Invented fixtures only. No production IDs, sessions, corpus reads or credentials.
export const person = {
  id: "fixture-person",
  name: "Mira of the Lantern",
  entity_type: "npc",
  description: "Mira keeps the lantern lit beside the invented harbour.",
};
export const place = {
  id: "fixture-place",
  name: "Paper Harbour",
  entity_type: "location",
  description: "A harbour drawn for this fixture.",
};
export const chunk = {
  id: "fixture-chunk",
  chunk_id: "fixture-chunk",
  book_id: "fixture-book",
  seq: 1,
  section_path: "Lantern / Harbour",
  content:
    "THE HARBOUR\nMira keeps a lantern at Paper Harbour.\n- Carry a map.\n- Watch the tide.",
  touched: [],
};
export const books = [
  {
    book_id: "fixture-book",
    display_name: "The Lantern Atlas",
    book_kind: "setting",
    chunk_count: 2,
    entity_count: 2,
    image_count: 0,
    copyrighted_content: false,
    latest_chunk_at: "2026-10-18T00:00:00Z",
  },
  {
    book_id: "fixture-locked",
    display_name: "The Closed Ledger",
    book_kind: "rulebook",
    chunk_count: 0,
    entity_count: 0,
    image_count: 0,
    copyrighted_content: true,
  },
];
export const sections = [
  {
    title: "Harbour",
    depth: 0,
    parent_path: null,
    section_path: "Lantern / Harbour",
    section_hierarchy: ["Lantern", "Harbour"],
    raw_section_paths: ["Lantern / Harbour"],
    first_chunk_id: "fixture-chunk",
    first_seq: 1,
    chunk_count: 1,
  },
];
export const touched = [
  { kind: "entity", id: person.id, name: person.name, entity_type: "npc" },
  {
    kind: "chunk",
    id: chunk.id,
    book_id: chunk.book_id,
    section_path: chunk.section_path,
    name: "Lantern / Harbour",
  },
];
export const messages = [
  { role: "user", content: "Who keeps the lantern?" },
  {
    role: "assistant",
    content: "Mira of the Lantern keeps it at Paper Harbour.",
    touched,
  },
];
export const adventure = {
  id: "fixture-adventure",
  name: "The Paper Tide",
  book_id: "fixture-book",
  book_display_name: "The Lantern Atlas",
  summary: "An invented expedition to the harbour.",
  level_range: "1-3",
  entity_count: 2,
  entities: [person, place],
};
export const graph = {
  nodes: [person, place],
  edges: [{ from: person.id, to: place.id, rel_type: "located_in" }],
  lens_counts: { world: 1, story: 1, quests: 0, rules: 0 },
};
export function routeData(state = "normal") {
  return {
    turnstileSiteKey: state === "unavailable" ? "" : "fixture-key",
    admitted: state !== "gate",
    initialMessages: state === "empty" ? [] : messages,
    messages: state === "empty" ? [] : messages,
    snapshotId: "fixture-share",
    items: state === "empty" ? [] : [chunk],
    nextCursor: null,
    locked: state === "locked",
    createdAt: "2026-10-18T00:00:00Z",
  };
}

// All calls, including the real Grimoire admission helper's POST, end here.
// Unexpected URLs fail closed instead of reaching a backend.
export function installNetwork(state = "normal", target = globalThis) {
  const requests = [];
  target.fetch = async (input, options = {}) => {
    const url = new URL(
      typeof input === "string" ? input : input.url,
      "http://fixture.local",
    );
    requests.push({ path: url.pathname, method: options.method ?? "GET" });
    const json = (body, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
    if (
      ["/app/grimoire/chat/session", "/app/grimoire/chat/fork"].includes(
        url.pathname,
      )
    )
      return json(
        state === "rejected" ? { ok: false } : { ok: true },
        state === "rejected" ? 403 : 200,
      );
    const path = url.pathname.replace(/^\/app\/grimoire\/api/, "");
    if (state === "error") return json({ detail: "Fixture unavailable" }, 503);
    if (state === "missing") return json({ detail: "not found" }, 404);
    if (path === "/books") return json(state === "empty" ? [] : books);
    if (path.endsWith("/sections")) return json(sections);
    if (path === "/adventures" || path.endsWith("/adventures"))
      return json(state === "empty" ? [] : [adventure]);
    if (path.startsWith("/adventures/")) return json(adventure);
    if (path === "/entities")
      return json({
        items: state === "empty" ? [] : [person, place],
        total: state === "empty" ? 0 : 2,
        next_cursor: null,
      });
    if (path.endsWith("/relationships"))
      return json([
        { direction: "out", rel_type: "located_in", entity: place },
      ]);
    if (path.endsWith("/mentions"))
      return json([
        {
          book_id: "fixture-book",
          chunk_id: "fixture-chunk",
          section_path: chunk.section_path,
          preview: "An invented source for the lantern.",
        },
      ]);
    if (path.startsWith("/entities/"))
      return json(path.endsWith(place.id) ? place : person);
    if (path === "/explore/ego")
      return json(state === "empty" ? { nodes: [], edges: [] } : graph);
    if (path.startsWith("/chunks/")) return json(chunk);
    if (url.pathname.endsWith("/read"))
      return json({ items: [], next_cursor: null });
    throw new Error(`Unexpected fixture request: ${url.pathname}`);
  };
  return requests;
}

// The vendor widget is simulated, while TurnstileGate and its admission helper
// are the production components. Clicking explicitly exercises the boundary.
export function installChallenge(target = window) {
  target.turnstile = {
    render(element, options) {
      const button = document.createElement("button");
      button.textContent = "Solve fixture challenge";
      button.onclick = () => options.callback("fixture-token");
      element.append(button);
      return "fixture-widget";
    },
    remove() {},
  };
}
