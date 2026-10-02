// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import NotesPanel from "./NotesPanel.svelte";

const mounted = [];
const campaignId = "11111111-1111-4111-8111-111111111111";
const note = (overrides = {}) => ({
  id: "22222222-2222-4222-8222-222222222222",
  kind: "character",
  title: "My discovery",
  markdown: "A **clue**",
  is_mine: true,
  can_edit: true,
  dm_readable: false,
  links: { entities: [], event_ids: [] },
  ...overrides,
});

async function panel(props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  // Keep native form navigation from leaving happy-dom during tab tests.
  target.addEventListener("submit", (event) => event.preventDefault());
  const instance = mount(NotesPanel, {
    target,
    props: { campaignId, notes: [note()], ...props },
  });
  mounted.push({ instance, target });
  await tick();
  return target;
}

afterEach(async () => {
  for (const { instance, target } of mounted.splice(0)) {
    await unmount(instance);
    target.remove();
  }
});

describe("NotesPanel", () => {
  it("switches Mine and Party and shows only the selected kind", async () => {
    const root = await panel({
      notes: [
        note(),
        note({ id: "party", kind: "party", title: "Party plan" }),
      ],
    });
    const [mine, party] = root.querySelectorAll('[role="tab"]');
    expect(mine.textContent.trim()).toBe("Mine");
    expect(root.querySelector("article h2").textContent).toBe("My discovery");
    party.click();
    await tick();
    expect(party.getAttribute("aria-selected")).toBe("true");
    expect(root.querySelector("article h2").textContent).toBe("Party plan");
    expect(
      root.querySelector('form[aria-label="Add note"] [name="kind"]').value,
    ).toBe("party");
    expect(root.querySelector('form[aria-label="Add note"] select')).toBeNull();
    mine.click();
    await tick();
    expect(root.querySelector("article h2").textContent).toBe("My discovery");
  });

  it("starts on Party when requested and forwards search and form actions", async () => {
    const root = await panel({
      initialKind: "party",
      query: "dragon",
      filterAction: "/notes",
      createAction: "?/add",
    });
    expect(
      root
        .querySelector('[role="tab"][value="party"]')
        .getAttribute("aria-selected"),
    ).toBe("true");
    const search = root.querySelector(".search");
    expect(search.getAttribute("action")).toBe("/notes");
    expect(Object.fromEntries(new FormData(search))).toEqual({
      kind: "party",
      q: "dragon",
    });
    expect(
      root.querySelector('form[aria-label="Add note"]').getAttribute("action"),
    ).toBe("?/add");
  });

  it("quick-add preserves title and markdown with a three-state sharing choice", async () => {
    const root = await panel();
    const form = root.querySelector('form[aria-label="Add note"]');
    form.querySelector('[name="title"]').value = "Found a map";
    form.querySelector('[name="markdown"]').value = "## North\nTravel tomorrow";
    const select = form.querySelector('select[name="dm_readable"]');
    expect(select).not.toBeNull();
    expect([...select.options].map((option) => option.textContent)).toEqual([
      "Campaign default",
      "Private",
      "Share with DM",
    ]);
    expect([...select.options].map((option) => option.value)).toEqual([
      "",
      "false",
      "true",
    ]);
    expect(Object.fromEntries(new FormData(form))).toEqual({
      campaign_id: campaignId,
      kind: "character",
      title: "Found a map",
      markdown: "## North\nTravel tomorrow",
      dm_readable: "",
    });
    for (const choice of ["false", "true", ""]) {
      select.value = choice;
      const fields = new FormData(form);
      expect(fields.get("title")).toBe("Found a map");
      expect(fields.get("markdown")).toBe("## North\nTravel tomorrow");
      expect(fields.get("dm_readable")).toBe(choice);
    }
  });

  it("labels the DM character tab Shared with you and permits only party quick-add", async () => {
    const root = await panel({
      isDm: true,
      notes: [note({ is_mine: false, can_edit: false })],
    });
    expect(
      root.querySelector('[value="character"][role="tab"]').textContent.trim(),
    ).toBe("Shared with you");
    expect(root.querySelector('form[aria-label="Add note"]')).toBeNull();
    expect(root.querySelector("details")).toBeNull();
    root.querySelector('[value="party"][role="tab"]').click();
    await tick();
    expect(
      root.querySelector('form[aria-label="Add note"] [name="kind"]').value,
    ).toBe("party");
  });

  it("renders raw HTML as text, rejects unsafe links, and uses resolved entity chips only", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    const root = await panel({
      notes: [
        note({
          markdown:
            '<img src=x onerror="alert(1)">\n\n[bad](javascript:alert) **safe**',
          links: {
            entities: [{ id: "resolved", name: "Mira", type: "npc" }],
            entity_ids: ["unresolved-secret"],
            event_ids: ["opaque-event"],
          },
        }),
      ],
    });
    expect(root.querySelector("article").textContent).toContain(
      '<img src=x onerror="alert(1)">',
    );
    expect(root.querySelector("article img")).toBeNull();
    expect(root.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(root.querySelector("article strong").textContent).toBe("safe");
    expect(root.querySelector(".chips").textContent.trim()).toBe("Mira");
    expect(root.textContent).not.toContain("unresolved-secret");
    expect(root.textContent).not.toContain("opaque-event");
    expect(fetch).not.toHaveBeenCalled();
    fetch.mockRestore();
  });

  it("shows editing only for can_edit and sharing only for the character author", async () => {
    const root = await panel({
      notes: [
        note({ can_edit: false }),
        note({
          id: "other",
          title: "Shared discovery",
          is_mine: false,
          can_edit: true,
        }),
      ],
    });
    const articles = root.querySelectorAll("article");
    expect(articles[0].querySelector("details")).toBeNull();
    const edit = articles[1].querySelector('form[action="?/update"]');
    expect(new FormData(edit).get("note_id")).toBe("other");
    expect(edit.querySelector('[name="dm_readable"]')).toBeNull();
    expect(articles[1].querySelector('form[action="?/delete"]')).not.toBeNull();
  });

  it("keeps author sharing editable and party sharing absent", async () => {
    const root = await panel();
    const edit = root.querySelector('form[action="?/update"]');
    expect(new FormData(edit).get("dm_readable")).toBe("false");
    const select = edit.querySelector("select");
    select.value = "true";
    expect(new FormData(edit).get("dm_readable")).toBe("true");
    const partyRoot = await panel({
      initialKind: "party",
      isDm: true,
      notes: [note({ kind: "party", is_mine: false })],
    });
    expect(
      partyRoot.querySelector('form[action="?/update"] [name="dm_readable"]'),
    ).toBeNull();
  });

  it("hides quick-add for characterless players and surfaces server errors", async () => {
    const root = await panel({
      canCreate: false,
      error: "note edit not permitted",
    });
    expect(root.querySelector('form[aria-label="Add note"]')).toBeNull();
    expect(root.querySelector('[role="alert"]').textContent).toBe(
      "note edit not permitted",
    );
    root.querySelector('[value="party"][role="tab"]').click();
    await tick();
    expect(root.querySelector('form[aria-label="Add note"]')).toBeNull();
  });
});
