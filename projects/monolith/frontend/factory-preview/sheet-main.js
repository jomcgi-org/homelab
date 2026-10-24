import "../../../design-system/tokens/contract.css";
import "./fonts.css";
import { mount } from "svelte";
import App from "./SheetRoutes.svelte";
import { payloads } from "./fixtures.js";
window.__fixturePolls = 0;
window.__fixtureLinks = [];
window.__fixtureUnexpectedFetches = [];
window.fetch = async (input) => {
  const values = payloads();
  if (!(input in values)) {
    window.__fixtureUnexpectedFetches.push(String(input));
    throw new Error(`Unexpected fixture fetch: ${input}`);
  }
  return new Response(JSON.stringify(values[input]), {
    headers: { "content-type": "application/json" },
  });
};
document.addEventListener("click", (event) => {
  const anchor = event.target.closest("a[href]");
  if (anchor && !anchor.getAttribute("href").startsWith("#")) {
    event.preventDefault();
    window.__fixtureLinks.push(anchor.getAttribute("href"));
  }
});
document.addEventListener("submit", (event) => event.preventDefault());
mount(App, { target: document.getElementById("app") });
