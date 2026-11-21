import { mount, unmount } from "svelte";
import Page from "./Page.svelte";
export function showMap(target) {
  const page = mount(Page, { target });
  return () => {
    void unmount(page);
  };
}
