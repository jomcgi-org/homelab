// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { writable } from "svelte/store";
import Route from "../../../../test/MovingRoute.svelte";
import {
  readyData,
  movingState,
  clock,
} from "../../../../test/moving-fixtures.js";
import { page } from "$app/stores";
import { goto } from "$app/navigation";

vi.mock("$app/stores", () => ({ page: writable({}) }));
vi.mock("$app/navigation", () => ({ goto: vi.fn() }));

let instance, target, phone;
const json = (value, status = 200) =>
  new Response(JSON.stringify(value), { status });
const button = (root, label) =>
  [...root.querySelectorAll("button")].find(
    (item) => item.textContent.trim() === label,
  );
async function render(data = readyData(), query = "") {
  page.set({
    url: new URL(`https://friends.example.test/moving${query}`),
    status: 200,
    error: null,
    data,
  });
  instance = mount(Route, { target, props: { data } });
  await tick();
}
async function submit(form) {
  form.dispatchEvent(
    new SubmitEvent("submit", { bubbles: true, cancelable: true }),
  );
  await tick();
}
beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  vi.setSystemTime(clock);
  phone = new EventTarget();
  phone.matches = false;
  vi.stubGlobal("matchMedia", (query) =>
    query.includes("700px")
      ? phone
      : Object.assign(new EventTarget(), { matches: false }),
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(() => {
      throw new Error("Unexpected local request");
    }),
  );
  target = document.createElement("div");
  document.body.append(target);
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("moving sheet composed route", () => {
  it.each(["forbidden", "unavailable"])(
    "keeps the %s access view, title and chrome",
    async (status) => {
      await render({ status, scope: "mine", mode: "", state: null });
      expect(
        target.querySelector("main.td.sheet .edition.access-panel"),
      ).not.toBeNull();
      expect(target.querySelector("h1").textContent).toBe(
        status === "forbidden"
          ? "This account is not recognised"
          : "The plan is unavailable",
      );
      expect(target.querySelector(".md-nav, .app, dialog")).toBeNull();
      expect(target.querySelectorAll("button.scheme")).toHaveLength(1);
      expect(document.title).toBe("Crossing | Moving planner");
      expect(
        document.querySelector('link[href*="family=Schibsted+Grotesk"]'),
      ).not.toBeNull();
      expect(document.querySelector('meta[name="description"]').content).toBe(
        "Crossing, a shared moving plan for the road ahead.",
      );
      target.querySelector("button.scheme").click();
      await tick();
      expect(localStorage.getItem("td-theme")).toBe("dark");
      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it("renders every dashboard panel with real chrome and timeline geometry", async () => {
    await render();
    expect(target.querySelector(".md-nav")).toBeNull();
    expect(target.querySelectorAll("main.td.sheet")).toHaveLength(1);
    expect(target.querySelectorAll("button.scheme")).toHaveLength(1);
    for (const name of [
      "c-hero",
      "c-today",
      "card-plot",
      "c-agenda",
      "c-prog",
      "c-collide",
      "c-dock",
    ])
      expect(target.querySelector(`.edition.${name}`)).not.toBeNull();
    expect(target.querySelectorAll("dialog")).toHaveLength(5);
    expect(target.querySelectorAll(".bar")).toHaveLength(
      movingState().spans.length,
    );
    expect(
      parseFloat(target.querySelector(".plot").style.height),
    ).toBeGreaterThan(200);
    expect(
      target
        .querySelector('[role="progressbar"]')
        .getAttribute("aria-valuenow"),
    ).toBe("25");
  });

  it("preserves unrelated URL parameters and navigation options", async () => {
    await render(readyData(), "?scope=mine&fixture=populated");
    button(target, "All").click();
    let [url, options] = goto.mock.lastCall;
    expect(url.pathname + url.search).toBe(
      "/moving?scope=all&fixture=populated",
    );
    expect(options).toEqual({ keepFocus: true, noScroll: true });
    button(target, "Manage").click();
    [url] = goto.mock.lastCall;
    expect(url.searchParams.get("mode")).toBe("manage");
    expect(url.searchParams.get("scope")).toBe("mine");
  });

  it("updates phone tabs after a viewport change and removes its listener", async () => {
    const remove = vi.spyOn(phone, "removeEventListener");
    await render();
    phone.matches = true;
    phone.dispatchEvent(new Event("change"));
    await tick();
    expect(target.querySelector(".app.phone")).not.toBeNull();
    for (const [name, value] of [
      ["Plan", "plan"],
      ["More", "more"],
      ["Today", "today"],
    ]) {
      [...target.querySelectorAll(".tabbar button")]
        .find((item) => item.textContent.trim().startsWith(name))
        .click();
      await tick();
      expect(target.querySelector(".app").dataset.mtab).toBe(value);
    }
    phone.matches = false;
    phone.dispatchEvent(new Event("change"));
    await tick();
    expect(target.querySelector(".app.phone")).toBeNull();
    await unmount(instance);
    instance = undefined;
    expect(remove).toHaveBeenCalledWith("change", expect.any(Function));
  });

  it("rolls back a pending failed task and allows retry", async () => {
    let finish;
    fetch.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    await render();
    const task = () => target.querySelector("#task-task-pack");
    task().dispatchEvent(new Event("change", { bubbles: true }));
    await tick();
    expect(task().disabled).toBe(true);
    expect(task().checked).toBe(true);
    task().dispatchEvent(new Event("change", { bubbles: true }));
    expect(fetch).toHaveBeenCalledOnce();
    finish(json({}, 503));
    await vi.waitFor(() => expect(task().disabled).toBe(false));
    expect(task().checked).toBe(false);
    expect(target.querySelector(".task-error").textContent).toContain(
      "Please try again",
    );
    fetch.mockResolvedValue(json({}));
    task().dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(task().disabled).toBe(false));
    expect(task().checked).toBe(true);
    expect(target.querySelector(".task-error")).toBeNull();
    expect(fetch.mock.lastCall[0]).toBe("/api/moving/tasks/task-pack/done");
  });

  it("opens each detail dialog, closes its predecessor and handles backdrop/Close", async () => {
    await render();
    const dialogs = [...target.querySelectorAll("dialog")];
    for (const [index, id] of [
      [0, "sell-heading"],
      [1, "calendar-heading"],
      [2, "roles-heading"],
      [3, "tasks-heading"],
      [4, "chat-heading"],
    ]) {
      target.querySelectorAll(".dockbtn")[index].click();
      await tick();
      expect(dialogs.filter((item) => item.open)).toHaveLength(1);
      expect(target.querySelector(`dialog[aria-labelledby="${id}"]`).open).toBe(
        true,
      );
    }
    const chat = target.querySelector(".chat-dialog");
    chat.querySelector(".dlg-body").click();
    expect(chat.open).toBe(true);
    chat.click();
    expect(chat.open).toBe(false);
    target.querySelectorAll(".dockbtn")[4].click();
    chat.querySelector(".x").click();
    expect(chat.open).toBe(false);
  });

  it("keeps every empty branch accessible", async () => {
    await render(readyData({ state: movingState(true) }));
    for (const copy of [
      "Nothing waiting in the next seven days.",
      "No dated plan items yet.",
      "No timeline dates yet.",
      "No legs scheduled.",
      "No date collisions.",
      "No tasks yet.",
      "Nothing is marked to sell.",
    ])
      expect(target.textContent).toContain(copy);
    expect(
      target
        .querySelector('[role="progressbar"]')
        .getAttribute("aria-valuenow"),
    ).toBe("0");
  });

  it("binds management creates and edits back to the dashboard and dialogs", async () => {
    await render(
      readyData({ mode: "manage", scope: "all" }),
      "?mode=manage&scope=mine&keep=yes",
    );
    expect(target.querySelector(".grid.manage-hidden")).not.toBeNull();
    expect(target.querySelectorAll(".manage-section.edition")).toHaveLength(5);
    const original = movingState().tasks[0];
    fetch.mockResolvedValue(
      json({ ...original, title: "Updated by management" }),
    );
    const row = [...target.querySelectorAll(".tasks-manage .manage-row")].find(
      (item) => item.textContent.includes(original.title),
    );
    button(row, "Edit").click();
    await tick();
    const edit = target.querySelector(".tasks-manage .edit-row");
    edit.elements.title.value = "Updated by management";
    await submit(edit);
    await vi.waitFor(() =>
      expect(
        target.querySelector("#task-task-pack").closest("li").textContent,
      ).toContain("Updated by management"),
    );
    const newSpan = {
      ...movingState().spans[2],
      id: "new-span",
      label: "New work date",
    };
    fetch.mockResolvedValue(json(newSpan, 201));
    const add = target.querySelector('form[aria-label="Add span"]');
    for (const key of ["kind", "label", "starts_on", "ends_on", "owner"])
      add.elements[key].value = newSpan[key];
    await submit(add);
    await vi.waitFor(() =>
      expect(target.querySelector(".card-plot").textContent).toContain(
        "New work date",
      ),
    );
    button(target, "Back to plan").click();
    expect(goto.mock.lastCall[0].search).toBe("?scope=mine&keep=yes");
  });
});
