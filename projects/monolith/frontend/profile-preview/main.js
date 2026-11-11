import { mount } from "svelte";
import "../preview/fonts.css";
// Model the stylesheet retained after visiting the separate agents story.
import "../src/lib/public/ember/ember.css";
import App from "./App.svelte";
mount(App, { target: document.getElementById("app") });
