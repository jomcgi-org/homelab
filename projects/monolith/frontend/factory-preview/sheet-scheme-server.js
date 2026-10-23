import { render } from "svelte/server";
import Preference from "../src/lib/public/components/SchemePreference.svelte";
import Toggle from "../src/lib/public/components/SchemeToggle.svelte";

export function renderSchemes() {
  return { preference: render(Preference), toggle: render(Toggle) };
}
