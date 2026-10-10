// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import InventoryPanel from "./InventoryPanel.svelte";

const pc = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const other = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const ropeId = "11111111-1111-4111-8111-111111111111";
const potionId = "22222222-2222-4222-8222-222222222222";
const characters = [
  { id: pc, character_name: "Aria" },
  { id: other, character_name: "Bram" },
];
const items = [
  {
    id: ropeId,
    owner: pc,
    is_mine: true,
    name: "Rope",
    quantity: 5,
    notes: "Silk",
    hidden_from_party: false,
    entity: { id: other, name: "Moon rope", type: "item" },
  },
  {
    id: potionId,
    owner: "party",
    is_mine: false,
    name: "Potion",
    quantity: 3,
    notes: "",
    hidden_from_party: false,
    entity: null,
  },
];
const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status });
let instance;
async function settle() {
  for (let i = 0; i < 10; i++) await tick();
}
async function render(props = {}, rows = items, fail = false) {
  const fetch = vi.fn(async (url, init) => {
    if (init?.method === "POST")
      return fail ? json({ error: "Save failed" }, 400) : json({});
    if (url.includes("inventory=changes"))
      return json([
        {
          id: "audit",
          action: "move",
          delta: -2,
          quantity_after: 3,
          reason: "Shared",
          changes: { owner: { from: "you", to: "party" } },
          created_at: "2026-10-10T12:00:00Z",
        },
      ]);
    return json(rows);
  });
  vi.stubGlobal("fetch", fetch);
  instance = mount(InventoryPanel, {
    target: document.body,
    props: { endpoint: "/state", characters: [characters[0]], ...props },
  });
  await settle();
  return fetch;
}
const button = (name) =>
  [...document.querySelectorAll("button")].find(
    (node) => node.textContent.trim() === name,
  );
const input = (label) =>
  [...document.querySelectorAll("label")]
    .find(
      (node) =>
        [...node.childNodes]
          .filter((child) => child.nodeType === 3)
          .map((child) => child.textContent)
          .join("")
          .trim() === label,
    )
    ?.querySelector("input, textarea, select");
async function fill(label, value) {
  const node = input(label);
  expect(node, label).toBeTruthy();
  node.value = value;
  if (node.tagName === "SELECT") {
    // happy-dom does not match selected options with :checked. Svelte's
    // change binding reads that selector, so supply the browser's result.
    const query = node.querySelector.bind(node);
    vi.spyOn(node, "querySelector").mockImplementation((selector) =>
      selector === ":checked"
        ? node.selectedOptions[0] || null
        : query(selector),
    );
  }
  node.dispatchEvent(
    new Event(node.tagName === "SELECT" ? "change" : "input", {
      bubbles: true,
    }),
  );
  await settle();
}
async function submit(name) {
  const node = button(name);
  expect(node, name).toBeTruthy();
  node
    .closest("form")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await settle();
}
const posted = (fetch) =>
  fetch.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([, init]) => JSON.parse(init.body));
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("inventory controls", () => {
  it("limits players to quantity and pool moves without owner identity sections", async () => {
    await render();
    expect(document.querySelector('[aria-label="Party pool"]')).toBeTruthy();
    expect(
      document.querySelector('[aria-label="Your inventory"]'),
    ).toBeTruthy();
    expect(document.body.textContent).not.toMatch(
      /Give item|Delete Rope|Name for Rope|Hide Rope|Aria|Bram/,
    );
    expect(document.querySelector('input[type="checkbox"]')).toBeNull();
    expect(input("Item name")).toBeUndefined();
    expect(button("Take Potion")).toBeTruthy();
    expect(button("Move Rope to party pool")).toBeTruthy();
    expect(button("Save Potion")).toBeUndefined();
  });

  it("gives the DM named sections and give, edit, hidden, move and delete controls", async () => {
    await render({ dm: true, characters });
    for (const name of ["Party pool", "Aria", "Bram"])
      expect(document.querySelector(`[aria-label="${name}"]`)).toBeTruthy();
    expect(input("Item name")).toBeTruthy();
    expect(input("Name for Rope")).toBeTruthy();
    expect(input("Hide Rope from party")).toBeTruthy();
    expect(input("Move Rope to").options.length).toBe(3);
    expect(button("Delete Rope")).toBeTruthy();
  });

  it("shows a text hidden badge, with no player move for a hidden item", async () => {
    await render({}, [{ ...items[0], hidden_from_party: true }]);
    expect(document.querySelector(".badge").textContent).toBe(
      "Hidden from party",
    );
    expect(button("Save Rope")).toBeTruthy();
    expect(button("Move Rope to party pool")).toBeUndefined();
  });

  it("sends player move, take and quantity operations and refreshes each time", async () => {
    const fetch = await render();
    await fill("Move quantity for Rope", "2");
    await fill("Move reason for Rope (optional)", "Share");
    await submit("Move Rope to party pool");
    await fill("Take quantity for Potion", "1");
    await submit("Take Potion");
    await fill("Quantity for Rope", "0");
    await fill("Reason for Rope (optional)", "Consumed");
    await submit("Save Rope");
    expect(posted(fetch)).toEqual([
      {
        operation: "moveItem",
        itemId: ropeId,
        owner: "party",
        quantity: 2,
        reason: "Share",
      },
      {
        operation: "moveItem",
        itemId: potionId,
        owner: pc,
        quantity: 1,
        reason: "",
      },
      {
        operation: "updateItem",
        itemId: ropeId,
        quantity: 0,
        reason: "Consumed",
      },
    ]);
    expect(
      fetch.mock.calls.filter(([url]) => url === "/state?inventory=items"),
    ).toHaveLength(4);
  });

  it("posts giveItem with reveal prefill and resets only after success", async () => {
    const prefill = {
      owner: pc,
      name: "Silver key",
      entity_id: other,
      source_event_id: ropeId,
    };
    const fetch = await render({ dm: true, characters, prefill });
    expect(input("Give to").value).toBe(pc);
    await fill("Quantity", "2");
    await fill("Notes", "Reward");
    await fill("Reason (optional)", "Found in chest");
    input("Hidden from party").checked = true;
    input("Hidden from party").dispatchEvent(
      new Event("change", { bubbles: true }),
    );
    await submit("Give Silver key");
    expect(posted(fetch)).toEqual([
      {
        operation: "giveItem",
        ...prefill,
        quantity: 2,
        notes: "Reward",
        hidden_from_party: true,
        reason: "Found in chest",
      },
    ]);
    expect(input("Item name").value).toBe("");
  });

  it("retains failed form input with an alert", async () => {
    const fetch = await render({ dm: true }, items, true);
    await fill("Item name", "Keep this name");
    await fill("Notes", "Keep notes");
    await submit("Give Keep this name");
    expect(document.querySelector('[role="alert"]')).toBeTruthy();
    expect(document.querySelector('[role="alert"]').textContent).toBe(
      "Save failed",
    );
    expect(input("Item name").value).toBe("Keep this name");
    expect(input("Notes").value).toBe("Keep notes");
    expect(posted(fetch)).toHaveLength(1);
  });

  it("retains a player's failed quantity and reason", async () => {
    await render({}, items, true);
    await fill("Quantity for Rope", "4");
    await fill("Reason for Rope (optional)", "Used a length");
    await submit("Save Rope");
    expect(document.querySelector('[role="alert"]')).toBeTruthy();
    expect(document.querySelector('[role="alert"]').textContent).toBe(
      "Save failed",
    );
    expect(input("Quantity for Rope").value).toBe("4");
    expect(input("Reason for Rope (optional)").value).toBe("Used a length");
  });

  it("disables submissions and ignores duplicates while a mutation is pending", async () => {
    const fetch = await render({ dm: true });
    let finish;
    fetch.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await fill("Item name", "Torch");
    await submit("Give Torch");
    expect(
      document
        .querySelector('[aria-label="Campaign inventory"]')
        .getAttribute("aria-busy"),
    ).toBe("true");
    for (const node of document.querySelectorAll("form button"))
      expect(node.disabled).toBe(true);
    await submit("Give Torch");
    expect(posted(fetch)).toHaveLength(1);
    finish(json({}));
    await settle();
    expect(button("Give item").disabled).toBe(false);
  });

  it("loads history on disclosure and leaves projected owner values intact", async () => {
    const openKnowledge = vi.fn();
    const fetch = await render({ openKnowledge });
    button("Moon rope").click();
    expect(openKnowledge).toHaveBeenCalledWith(other);
    const details = [...document.querySelectorAll("details")].find(
      (node) =>
        node.querySelector("summary").textContent === "History for Rope",
    );
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
    await settle();
    expect(
      fetch.mock.calls.some(
        ([url]) => url === `/state?inventory=changes&item=${ropeId}`,
      ),
    ).toBe(true);
    expect(details.textContent.replace(/\s+/g, " ")).toContain(
      "move: delta -2, quantity after 3.",
    );
    expect(details.textContent).toContain("Owner: you to party.");
    expect(details.textContent).toContain("Shared");
    expect(details.querySelector("time").dateTime).toBe("2026-10-10T12:00:00Z");
  });

  it("edits and moves DM items, and confirms before deleting", async () => {
    const fetch = await render({ dm: true, characters });
    await fill("Name for Rope", "Long rope");
    await fill("Notes for Rope", "Frayed");
    await submit("Save Rope");
    await fill("Move Rope to", other);
    await submit("Move Rope");
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    button("Delete Rope").click();
    await settle();
    expect(posted(fetch)).toHaveLength(2);
    confirm.mockReturnValue(true);
    button("Delete Rope").click();
    await settle();
    expect(confirm).toHaveBeenCalledWith(
      "Delete Rope? Its audit history is retained.",
    );
    expect(posted(fetch)).toEqual([
      {
        operation: "updateItem",
        itemId: ropeId,
        name: "Long rope",
        notes: "Frayed",
        quantity: 5,
        hidden_from_party: false,
        reason: "",
      },
      {
        operation: "moveItem",
        itemId: ropeId,
        owner: other,
        quantity: 5,
        reason: "",
      },
      { operation: "deleteItem", itemId: ropeId },
    ]);
  });
});
