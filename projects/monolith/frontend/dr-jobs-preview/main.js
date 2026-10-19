import { mount, unmount } from "svelte";
import App from "./App.svelte";
import { fixtureData } from "./fixtures.js";
import { calls } from "./navigation.js";
import "../preview/fonts.css";

const url = new URL(location.href);
const app = mount(App, {
  target: document.getElementById("app"),
  props: { url: url.href, data: fixtureData(url.searchParams.get("fixture")) },
});
// Only the preview exposes diagnostics and teardown. Production imports none
// of this directory, including its invented data.
window.drJobsFixture = { calls, dispose: () => unmount(app) };
document.addEventListener("click", (event) => {
  if (event.target.closest("a[href]")) event.preventDefault();
});
