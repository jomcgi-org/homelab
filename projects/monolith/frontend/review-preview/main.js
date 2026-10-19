import { mount } from "svelte";
import "../src/lib/global.css";
import "../src/lib/public/styles/design-system.css";
import "../preview/fonts.css";
import App from "./App.svelte";

// No private loader, API client, form action or request shim is included.
mount(App, { target: document.getElementById("app") });
