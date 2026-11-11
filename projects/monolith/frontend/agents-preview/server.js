import { render } from "svelte/server";
import App from "../profile-preview/App.svelte";

export function renderFixture(banner = false) {
  return render(App, {
    props: {
      initialUrl: `https://jomcgi.dev/ember/agents${banner ? "?banner=1" : ""}`,
    },
  });
}
