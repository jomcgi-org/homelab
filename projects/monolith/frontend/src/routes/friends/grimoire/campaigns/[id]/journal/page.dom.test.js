// @vitest-environment happy-dom
import { afterEach, expect, it } from "vitest";
import { mount, unmount } from "svelte";
import Page from "./+page.svelte";
import { settle } from "$lib/grimoire/test-helpers.js";

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  document.body.innerHTML = "";
});
it("renders a labelled character knowledge search in the journal tab", async () => {
  instance = mount(Page, {
    target: document.body,
    props: {
      data: {
        campaign: {
          id: "11111111-1111-4111-8111-111111111111",
          name: "Adventure",
        },
        view: "mine",
        journal: { sessions: [], next_cursor: null },
      },
    },
  });
  await settle();
  const input = document.querySelector('input[type="search"]');
  expect(input).not.toBeNull();
  expect(document.querySelector(`label[for="${input.id}"]`).textContent).toBe(
    "Search what your character knows",
  );
  expect(document.body.textContent).toContain("No sessions recorded yet.");
  expect(
    document.querySelector('form[aria-label="Journal audience"]'),
  ).not.toBeNull();
});
