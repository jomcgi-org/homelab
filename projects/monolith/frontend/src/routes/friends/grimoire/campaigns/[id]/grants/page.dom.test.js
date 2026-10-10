// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, unmount } from "svelte";
import Page from "./+page.svelte";
import { buttonByText, settle } from "$lib/grimoire/test-helpers.js";

const pcs = [
  { id: "pc-a", character_name: "Aria" },
  { id: "pc-b", character_name: "Bram" },
];
const entities = [
  { id: "e1", name: "Mara", entity_type: "npc", is_global: false },
  { id: "e2", name: "Old Road", entity_type: "location", is_global: true },
  { id: "e3", name: "Forbiddance", entity_type: "spell", is_global: false },
];
const data = {
  campaign: { id: "camp", name: "Adventure" },
  characters: pcs,
  sessions: [],
  entities,
  grants: [
    { id: "g1", entity_id: "e1", player_character_id: "pc-a", grant_scope: "full" },
    { id: "g2", entity_id: "e1", player_character_id: "pc-b", grant_scope: "partial" },
    { id: "g3", entity_id: "e3", player_character_id: "pc-a", grant_scope: "name_only" },
  ],
};

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

describe("grants matrix", () => {
  it("labels every scope with visible text, not colour alone", async () => {
    vi.stubGlobal("fetch", vi.fn());
    instance = mount(Page, { target: document.body, props: { data } });
    await settle();
    const badge = (label) =>
      document.querySelector(`button[aria-label="${label}"] .badge`);
    expect(badge("Edit Mara for Aria").textContent).toBe("Full");
    expect(badge("Edit Mara for Aria").dataset.scope).toBe("full");
    expect(badge("Edit Mara for Bram").textContent).toBe("Partial");
    expect(badge("Edit Forbiddance for Aria").textContent).toBe("Name only");
    expect(badge("Edit Forbiddance for Bram").textContent).toBe("Not granted");
    expect(badge("Edit Old Road for Aria").textContent).toBe("Full by default");
  });

  it("offers the reveal panel for the DM against the session endpoint", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ items: [] })));
    vi.stubGlobal("fetch", fetch);
    instance = mount(Page, { target: document.body, props: { data } });
    await settle();
    buttonByText(document.body, "Reveal knowledge").click();
    await settle();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    // Both choices of recipient come from the page load's characters.
    const options = [...document.querySelectorAll("select option")].map(
      (option) => option.textContent,
    );
    expect(options).toEqual(expect.arrayContaining(["Aria", "Bram"]));
    document
      .querySelector('[role="dialog"] form')
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await settle();
    expect(fetch.mock.calls[0][0]).toBe(
      "/grimoire/campaigns/camp/session/state?q=",
    );
  });
});
