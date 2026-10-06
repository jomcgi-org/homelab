import { render } from "svelte/server";
import ControlsFixture from "./ControlsFixture.svelte";

export function renderFixture(props = {}) {
  return render(ControlsFixture, { props });
}
