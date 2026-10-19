import { mount } from "svelte";
import App from "./App.svelte";
import "../preview/fonts.css";
import { history as historicalData } from "../test/stars/fixtures.js";
import { Map } from "maplibre-gl";

// Observe the real map for camera/source/paint assertions. No renderer or
// geometry is substituted, and this hook is absent from production builds.
const addControl = Map.prototype.addControl;
Map.prototype.addControl = function (...args) {
  window.starsFixtureMap = this;
  return addControl.apply(this, args);
};

const realFetch = window.fetch.bind(window);
window.fetch = (input, options) => {
  if (input === "/app/stars/history-map") {
    return Promise.resolve(
      new Response(JSON.stringify(historicalData), {
        headers: { "Content-Type": "application/json" },
      }),
    );
  }
  return realFetch(input, options);
};
mount(App, { target: document.getElementById("app") });
