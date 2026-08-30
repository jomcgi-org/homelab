// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import ManagePanel from "./ManagePanel.svelte";
import { patchDiff } from "./moving.js";

const mounted = [];

const task = (overrides = {}) => ({
  id: "task-1",
  track: "admin",
  title: "Pack boxes",
  note: "Keep dry",
  owner: "both",
  due_on: "2026-09-01",
  value_cad: 25,
  done_at: null,
  created_at: "2026-08-20T10:00:00Z",
  ...overrides,
});

const span = (id, label) => ({
  id,
  kind: "move",
  label,
  starts_on: "2026-09-01",
  ends_on: "2026-09-02",
  owner: "both",
});

function response(body, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => (resolve = done));
  return { promise, resolve };
}

async function render(overrides = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(ManagePanel, {
    target,
    props: {
      spans: [],
      milestones: [],
      tasks: [],
      roles: [],
      collisions: [],
      viewer: "joe",
      ...overrides,
    },
  });
  mounted.push({ component, target });
  await tick();
  return target;
}

function rowButton(target, section, label, action) {
  const rows = target.querySelectorAll(`${section} .manage-row`);
  const row = [...rows].find((item) => item.textContent.includes(label));
  return [...row.querySelectorAll("button")].find(
    (button) => button.textContent === action,
  );
}

async function submit(form) {
  form.dispatchEvent(
    new SubmitEvent("submit", { bubbles: true, cancelable: true }),
  );
  await tick();
}

beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

afterEach(async () => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("moving manage panel", () => {
  it("saves entity with only changed fields", async () => {
    const original = task();
    fetch.mockResolvedValue(response({ ...original, due_on: null }));
    const target = await render({ tasks: [original] });
    rowButton(target, ".tasks-manage", original.title, "Edit").click();
    await tick();
    const form = target.querySelector(".tasks-manage .edit-row");
    form.elements.due_on.value = "";
    await submit(form);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    const patch = JSON.parse(fetch.mock.calls[0][1].body);

    expect(patch).toEqual(
      patchDiff({ due_on: original.due_on }, { due_on: null }, ["due_on"]),
    );
  });

  it("restores pre-edit state on failed PATCH", async () => {
    const original = task();
    fetch.mockResolvedValue(response({ detail: "Invalid date" }, 400));
    const target = await render({ tasks: [original] });
    rowButton(target, ".tasks-manage", original.title, "Edit").click();
    await tick();
    const form = target.querySelector(".tasks-manage .edit-row");
    form.elements.title.value = "Changed title";
    await submit(form);
    await vi.waitFor(() =>
      expect(target.querySelector('[role="alert"]')?.textContent).toContain(
        "Invalid date",
      ),
    );

    expect(target.textContent).toContain(original.title);
    expect(target.textContent).not.toContain("Changed title");
    expect(target.querySelector(".tasks-manage .edit-row")).toBeNull();
  });

  it("preserves a pending add when delete rolls back", async () => {
    const add = deferred();
    const remove = deferred();
    fetch.mockImplementation((url, options) =>
      options.method === "DELETE" ? remove.promise : add.promise,
    );
    const target = await render({ tasks: [task()] });
    const addForm = target.querySelector('form[aria-label="Add task"]');
    addForm.elements.title.value = "Book movers";
    await submit(addForm);
    rowButton(target, ".tasks-manage", "Pack boxes", "Delete").click();
    remove.resolve(response({ detail: "Still referenced" }, 409));
    await vi.waitFor(() => expect(target.textContent).toContain("Pack boxes"));
    expect(target.textContent).toContain("Book movers");
    add.resolve(response(task({ id: "task-2", title: "Book movers" }), 201));
    await vi.waitFor(() =>
      expect(target.querySelectorAll(".tasks-manage .manage-row")).toHaveLength(
        4,
      ),
    );
  });

  it("restores a completed task without immutable fields", async () => {
    const original = task({ done_at: "2026-08-25T12:00:00Z" });
    const created = task({ id: "task-2", done_at: null });
    const completed = { ...created, done_at: "2026-08-29T12:00:00Z" };
    fetch
      .mockResolvedValueOnce(response(null, 204))
      .mockResolvedValueOnce(response(created, 201))
      .mockResolvedValueOnce(response(completed));
    const target = await render({ tasks: [original] });
    rowButton(target, ".tasks-manage", original.title, "Delete").click();
    await vi.waitFor(() => expect(target.textContent).toContain("Undo"));
    target.querySelector(".undo-strip button").click();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    const body = JSON.parse(fetch.mock.calls[1][1].body);

    expect(body).not.toHaveProperty("id");
    expect(body).not.toHaveProperty("created_at");
    expect(body).not.toHaveProperty("done_at");
    expect(fetch.mock.calls[2][0]).toBe("/api/moving/tasks/task-2/done");
    expect(target.textContent).toContain("Done");
  });

  it("keeps restored task visible when completion restore fails", async () => {
    const original = task({ done_at: "2026-08-25T12:00:00Z" });
    const created = task({ id: "task-2", done_at: null });
    fetch
      .mockResolvedValueOnce(response(null, 204))
      .mockResolvedValueOnce(response(created, 201))
      .mockResolvedValueOnce(response({ detail: "Unavailable" }, 503));
    const target = await render({ tasks: [original] });
    rowButton(target, ".tasks-manage", original.title, "Delete").click();
    await vi.waitFor(() => expect(target.textContent).toContain("Undo"));
    target.querySelector(".undo-strip button").click();
    await vi.waitFor(() =>
      expect(target.querySelector('[role="alert"]')?.textContent).toBe(
        "Failed to restore task completion state",
      ),
    );

    expect(target.textContent).toContain(original.title);
    expect(target.textContent).toContain("Open");
  });

  it("omits an empty acknowledgement note", async () => {
    const spans = [span("span-1", "Pack"), span("span-2", "Move")];
    const collisions = [
      {
        type: "span_span",
        item1_id: "span-1",
        item2_id: "span-2",
        overlaps_from: "2026-09-01",
        overlaps_to: "2026-09-02",
        acked_by: null,
        ack_note: null,
      },
    ];
    fetch.mockResolvedValue(response({ acked_by: "joe", note: null }));
    const target = await render({ spans, collisions });
    await submit(target.querySelector(".ack-form"));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());

    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({});
  });

  it("includes a non-empty acknowledgement note", async () => {
    const spans = [span("span-1", "Pack"), span("span-2", "Move")];
    const collisions = [
      {
        type: "span_span",
        item1_id: "span-1",
        item2_id: "span-2",
        overlaps_from: "2026-09-01",
        overlaps_to: "2026-09-02",
        acked_by: null,
        ack_note: null,
      },
    ];
    fetch.mockResolvedValue(response({ acked_by: "joe", note: "Heads up" }));
    const target = await render({ spans, collisions });
    const form = target.querySelector(".ack-form");
    form.elements.note.value = "Heads up";
    await submit(form);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());

    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      note: "Heads up",
    });
  });
});
