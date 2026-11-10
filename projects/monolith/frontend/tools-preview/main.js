import { mount } from "svelte";
import Harness from "./Harness.svelte";
import { reroute } from "../src/hooks.js";
import {
  chatEvents,
  qwenEvents,
  sse,
  stats,
  noteDetail,
  reviewData,
  notesData,
  grimoireData,
  qwenSession,
} from "./fixtures.js";
const params = new URLSearchParams(location.search);
const view = location.pathname.split("/").filter(Boolean)[0] ?? "review";
if (
  reroute({ url: new URL(`https://private.fixture.invalid/${view}`) }) !==
  `/private/${view}`
)
  throw new Error("Fixture route does not match production hooks");
window.fixtureCalls = [];
window.fetch = async (input, options = {}) => {
  const path = String(input);
  window.fixtureCalls.push({
    path,
    method: options.method ?? "GET",
    body:
      options.body instanceof FormData
        ? Object.fromEntries(options.body)
        : options.body,
  });
  if (params.has("networkError"))
    throw new Error("Synthetic network unavailable");
  if (path.includes("stats")) return Response.json(stats);
  if (path.includes("note_id=") || path.includes("/api/knowledge/notes/"))
    return Response.json(noteDetail);
  if (path.includes("qwen/chat"))
    return new Response(sse(qwenEvents), {
      headers: { "Content-Type": "text/event-stream" },
    });
  if (path === "/private/chat")
    return new Response(sse(chatEvents), {
      headers: { "Content-Type": "text/event-stream" },
    });
  if (path.startsWith("?/"))
    return Response.json({ type: "success", status: 200 });
  throw new Error(`Unexpected fixture network call: ${path}`);
};
if (view === "qwen" && !params.has("empty"))
  localStorage.setItem("qwen-chat-sessions-v1", JSON.stringify([qwenSession]));
let data;
if (params.has("empty")) {
  if (view === "review") data = { ...reviewData, items: [] };
  if (view === "notes")
    data = { graph: { ...notesData.graph, nodes: [], edges: [] } };
  if (view === "grimoire") data = { groups: [] };
}
if (view === "grimoire" && params.has("dm"))
  data = grimoireData("dm", "submitted");
if (view === "grimoire" && params.has("unavailable"))
  data = { unavailable: true, message: "Synthetic service unavailable" };
mount(Harness, {
  target: document.getElementById("app"),
  props: { view, data },
});
