import { render } from "svelte/server";
import App from "./App.svelte";
export function renderFixture(props) {
  return render(App, { props });
}
