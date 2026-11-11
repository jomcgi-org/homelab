import "../../../design-system/tokens/contract.css";
import "../src/lib/global.css";
import "../src/lib/public/styles/design-system.css";
import "../src/lib/public/styles/technical-drawing.css";
import "./fonts.css";
import { mount } from "svelte";
import App from "./App.svelte";
import { fixture, navigate } from "./state.svelte.js";
import { payloads } from "./fixtures.js";
import { createExecutionFixture } from "./execution-fixtures.js";

const execution = createExecutionFixture();
window.__executionFixture = execution;
window.EventSource = execution.EventSource;

// Only this known production browser request has a synthetic endpoint.
// Never fall through to network fetch; the CSP also denies all connections.
window.__fixtureUnexpectedFetches = [];
window.fetch = async (input, options) => {
  if (["execution", "drain"].includes(fixture.view))
    return execution.fetch(input, options);
  if (
    !["/slop/factory/search-index", "/slop/factory/data/board"].includes(input)
  ) {
    window.__fixtureUnexpectedFetches.push(String(input));
    throw new Error(`Unexpected fixture fetch: ${input}`);
  }
  return new Response(JSON.stringify(payloads(fixture.scenario)[input]), {
    status: fixture.scenario === "error" ? 503 : 200,
    headers: { "content-type": "application/json" },
  });
};

document.addEventListener(
  "click",
  (event) => {
    const anchor = event.target.closest("a[href]");
    if (!anchor) return;
    event.preventDefault();
    if (!navigate(anchor.href))
      document.getElementById("preview-scope").focus();
  },
  true,
);
document.addEventListener(
  "submit",
  (event) => {
    event.preventDefault();
    const form = event.target;
    const target = new URL(form.action);
    target.search = new URLSearchParams(new FormData(form));
    navigate(target);
  },
  true,
);

mount(App, { target: document.getElementById("app") });
window.__factoryFixtureReady = true;
