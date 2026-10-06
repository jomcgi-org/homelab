import { hydrate, tick } from "svelte";
import ThemeFixture from "./ThemeFixture.svelte";
import "@homelab/design-system/tokens/contract.css";
import "@homelab/design-system/tokens/technical-drawing.css";

const target = document.getElementById("app");
const snapshot = () =>
  JSON.stringify({
    text: target.textContent,
    attributes: [...target.querySelectorAll("*")].map((node) =>
      [...node.attributes].map((attribute) => [
        attribute.name,
        attribute.value,
      ]),
    ),
  });
const before = snapshot();
hydrate(ThemeFixture, { target, recover: false });
await tick();
if (before !== snapshot())
  throw new Error("Theme fixture hydration changed server content");
target.dataset.hydrated = "true";
