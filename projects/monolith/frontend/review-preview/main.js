import { mount } from "svelte";
import "../src/lib/global.css";
import "../src/lib/public/styles/technical-drawing.css";
import "../preview/fonts.css";
import App from "./App.svelte";

// The review components sit on the technical-drawing sheet, which follows
// the browser's colour-scheme preference on its own (no stored choice here).
// No private loader, API client, form action or request shim is included.
mount(App, { target: document.getElementById("app") });
