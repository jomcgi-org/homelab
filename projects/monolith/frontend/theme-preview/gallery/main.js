import { hydrate, tick } from "svelte";
import GalleryFixture from "./GalleryFixture.svelte";

const target = document.getElementById("app");
const nodes = [...target.querySelectorAll("*")];
const snapshot = () =>
  JSON.stringify({
    text: target.textContent,
    attributes: [...target.querySelectorAll("*")].map((node) =>
      [...node.attributes].map(({ name, value }) => [name, value]),
    ),
  });
const before = snapshot();
hydrate(GalleryFixture, { target, recover: false });
await tick();
const after = [...target.querySelectorAll("*")];
if (
  before !== snapshot() ||
  nodes.length !== after.length ||
  nodes.some((node, i) => node !== after[i])
)
  throw new Error("Gallery hydration changed server nodes or content");
target.dataset.hydrated = "true";
