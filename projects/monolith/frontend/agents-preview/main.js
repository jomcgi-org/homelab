import { hydrate, mount } from "svelte";
import "../preview/fonts.css";
import App from "../profile-preview/App.svelte";

const target = document.getElementById("app");
if (location.pathname === "/ember/agents") {
  hydrate(App, { target });
} else {
  // The fixture serves one SSR route. Other direct loads use the same
  // production components via the profile fixture's history adapter.
  target.replaceChildren();
  mount(App, { target });
}
