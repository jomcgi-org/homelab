// The root error node's preloaded legacy CSS reaches private production pages.
// Import it here too: a fixture must not conceal inherited cascade defects.
import "../src/lib/public/styles/design-system.css";
import "../factory-preview/fonts.css";
import { mount } from "svelte";
import App from "./App.svelte";
import {
  board,
  context,
  escalations,
  fixtureData,
  routes,
  workItem,
} from "./fixtures.js";
import { setRoute } from "./state.js";

const params = new URLSearchParams(location.search);
const view = params.get("view") ?? "board";
const scenario = params.get("scenario") ?? "populated";
if (!Object.hasOwn(routes, view)) throw new Error("Unknown synthetic view");
setRoute(
  view,
  params.get("task") ? `?task=${encodeURIComponent(params.get("task"))}` : "",
);
const state = (window.__privateFixture = {
  requests: [],
  unexpected: [],
  navigation: [],
  escalations: scenario === "empty" ? [] : escalations(),
  document: workItem(scenario === "empty"),
  outcomes: [],
  hold: false,
  release: null,
});
const response = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
// Every fetch is handled in memory. Unknown paths/methods never fall through.
window.fetch = async (input, init = {}) => {
  const url = String(input),
    method = init.method ?? "GET";
  state.requests.push({
    url,
    method,
    body: init.body ? JSON.parse(init.body) : null,
  });
  if (method === "GET" && /^\/factory(?:\?task=[^&]+)?$/.test(url))
    return response(
      board(scenario === "empty"),
      scenario === "unavailable" ? 503 : 200,
    );
  if (method === "GET" && url === "/factory/escalations")
    return response(
      { escalations: state.escalations },
      scenario === "unavailable" ? 503 : 200,
    );
  if (
    method === "GET" &&
    /^\/factory\/escalations\/context\/(31|32)$/.test(url)
  )
    return response(context(Number(url.split("/").at(-1))));
  if (method === "GET" && url === routes.item)
    return response(
      state.document,
      scenario === "unavailable" ? 503 : scenario === "missing" ? 404 : 200,
    );
  if (
    (method === "POST" &&
      /^\/factory\/escalations\/decisions\/(31|32)$/.test(url)) ||
    (method === "POST" && url === `${routes.item}/edges`) ||
    (method === "DELETE" &&
      /^\/factory\/work-items\/100001\/edges\/[1-6]$/.test(url))
  ) {
    if (state.hold)
      await new Promise((resolve) => {
        state.release = resolve;
      });
    const outcome = state.outcomes.shift() ?? "accepted";
    if (outcome === "transport") throw new Error("Synthetic dropped response");
    if (outcome === "refused")
      return response({ detail: "Synthetic definitive refusal" }, 409);
    if (outcome === "unknown")
      return response({ ok: false, reason: "Synthetic outcome unknown" });
    if (url.includes("/decisions/")) return response({ ok: true });
    if (method === "DELETE") {
      const id = Number(url.split("/").at(-1));
      state.document.edges_in = state.document.edges_in.filter(
        (edge) => edge.id !== id,
      );
      state.document.edges_out = state.document.edges_out.filter(
        (edge) => edge.id !== id,
      );
    }
    return response(state.document);
  }
  state.unexpected.push({ url, method });
  throw new Error(`Unexpected synthetic request: ${method} ${url}`);
};
document.addEventListener(
  "click",
  (event) => {
    const anchor = event.target.closest("a[href]");
    if (!anchor) return;
    event.preventDefault();
    const path = new URL(anchor.href).pathname;
    const destination = Object.keys(routes).find((key) => routes[key] === path);
    if (destination) {
      const url = new URL(location.href);
      url.search = new URLSearchParams({ view: destination, scenario });
      location.href = url;
    }
  },
  true,
);
mount(App, {
  target: document.getElementById("app"),
  props: { view, data: fixtureData(view, scenario) },
});
state.ready = true;
