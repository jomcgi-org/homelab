import { render } from "svelte/server";
import GalleryFixture from "./GalleryFixture.svelte";

export function renderFixture(props = {}) {
  return render(GalleryFixture, { props });
}
