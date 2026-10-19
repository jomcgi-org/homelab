import { mount } from "svelte";
import "../src/lib/global.css";
import "../src/lib/public/styles/design-system.css";
import "../preview/fonts.css";
import App from "./App.svelte";

// This private vocabulary uses body.dark, independent of the public scheme.
const scheme = matchMedia("(prefers-color-scheme: dark)");
const applyScheme = () =>
  document.body.classList.toggle("dark", scheme.matches);
applyScheme();
scheme.addEventListener("change", applyScheme);

// No private loader, API client, form action or request shim is included.
mount(App, { target: document.getElementById("app") });
