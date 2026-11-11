import { mount } from "svelte";
import "../preview/fonts.css";
import App from "./App.svelte";

mount(App, { target: document.getElementById("app") });
