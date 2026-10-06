import { render } from "svelte/server";
import ThemeFixture from "./ThemeFixture.svelte";
import "@homelab/design-system/tokens/technical-drawing.css";

export function renderFixture() {
  return render(ThemeFixture);
}
