// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { mount, tick, unmount } from "svelte";
import Launcher from "./Launcher.svelte";
import { RUN_LEXICON as P } from "./run-lexicon.js";

const mounted = [];

function pageSurface() {
  const url = new URL("./+page.svelte", import.meta.url);
  if (url.protocol === "file:") return readFileSync(url, "utf8");
  const pathname = decodeURIComponent(url.pathname);
  const candidates = [
    pathname.startsWith("/@fs/") ? pathname.slice(4) : null,
    resolve(process.cwd(), pathname.replace(/^\/+/, "")),
    resolve(process.cwd(), "+page.svelte"),
  ].filter(Boolean);
  const filename = candidates.find(existsSync);
  if (!filename) throw new Error("Unable to locate +page.svelte");
  return readFileSync(filename, "utf8");
}

async function render(onSubmit, overrides = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Launcher, {
    target,
    props: {
      session: {
        prompt: "Start this task",
        model: "",
        repo: "",
        branch: "",
      },
      models: ["luna"],
      summary: {
        items: [],
        count: 0,
        allCount: 0,
        sessionCount: 0,
        runCount: 0,
        spend: 0,
      },
      onSubmit,
      ...overrides,
    },
  });
  mounted.push({ component, target });
  await tick();
  return target;
}

afterEach(async () => {
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("launcher submit path", () => {
  test("uses the human decision kind as a recent run's ask word", async () => {
    const target = await render(vi.fn(), {
      summary: {
        items: [
          {
            kind: "run",
            id: "wf-1",
            activityAt: "2026-08-22T11:00:00Z",
            value: {
              workflow_id: "wf-1",
              title: "Push a branch",
              state: "blocked",
              cost_usd: 0,
              needs: { kind: "human", decision_kind: "push_gate" },
              shape: [
                {
                  key: "push_gate",
                  kind: "gate",
                  state: "blocked",
                },
              ],
            },
          },
        ],
        count: 1,
        allCount: 1,
        sessionCount: 0,
        runCount: 1,
        spend: 0,
      },
    });

    expect(target.textContent).toContain("Approve push");
  });

  test("form submit and command enter call the provided task creator", async () => {
    const createTask = vi.fn();
    const target = await render(createTask);

    target
      .querySelector("form")
      .dispatchEvent(new Event("submit", { bubbles: true }));
    target.querySelector("textarea").dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        metaKey: true,
        bubbles: true,
      }),
    );
    await tick();

    expect(createTask).toHaveBeenCalledTimes(2);
  });

  test("empty model option names the default model", async () => {
    const target = await render(vi.fn());
    const select = target.querySelector(
      `[aria-label="${P.labels.modelPicker}"]`,
    );
    expect(select.querySelector('option[value=""]').textContent).toBe(
      "luna (default)",
    );
  });

  test("the page wires both launchers and the New panel to createTask", () => {
    const pageSource = pageSurface();

    expect(pageSource.match(/onSubmit=\{createTask\}/g) ?? []).toHaveLength(2);
    expect(pageSource).toMatch(
      /class="new-panel"[\s\S]*?<form\s+onsubmit=\{\(event\) => \{\s*event\.preventDefault\(\);\s*createTask\(\);/,
    );
  });

  test("loads branches for a selected repo and submits the chosen branch", async () => {
    const createTask = vi.fn();
    const onLoadBranches = vi.fn();
    const session = {
      prompt: "Start this task",
      model: "",
      repo: "acme/app",
      branch: "main",
    };
    const target = await render(createTask, {
      session,
      repos: [{ id: "acme/app" }, { id: "acme/other" }],
      branches: [{ name: "main" }, { name: "feature" }],
      onLoadBranches,
    });
    const repo = target.querySelector(`[aria-label="${P.labels.repoWord}"]`);
    repo.value = "acme/other";
    repo.dispatchEvent(new Event("change", { bubbles: true }));
    await tick();

    expect(onLoadBranches).toHaveBeenCalledWith("acme/other");
    const branch = target.querySelector(
      `[aria-label="${P.labels.branchWord}"]`,
    );
    branch.value = "feature";
    branch.dispatchEvent(new Event("change", { bubbles: true }));
    target
      .querySelector("form")
      .dispatchEvent(new Event("submit", { bubbles: true }));
    await tick();

    expect(session.branch).toBe("feature");
    expect(createTask).toHaveBeenCalledTimes(1);
  });

  test("shows session and run totals with the shared Jump count", async () => {
    const target = await render(vi.fn(), {
      jumpCount: 9,
      summary: {
        items: [],
        count: 5,
        allCount: 7,
        sessionCount: 2,
        runCount: 3,
        spend: 1,
      },
    });

    const summary = target
      .querySelector(".recent-summary")
      .textContent.replace(/\s+/g, " ")
      .trim();
    expect(summary).toBe("7 days · 2 sessions · 3 runs · $1.00");
    expect(target.textContent).toContain("All 9 in Jump");
  });
});
