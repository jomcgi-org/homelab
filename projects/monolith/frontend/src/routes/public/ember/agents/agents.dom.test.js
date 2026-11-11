// @vitest-environment happy-dom
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "../../../../../test/ProfileRoute.svelte";
import { setPage } from "../../../../../test/profile-page.svelte.js";
import {
  CALLS,
  CHAT_SCHEDULE,
  PHASES,
} from "$lib/public/agentstory/timeline.js";
import { sessionRestoreMs } from "$lib/public/fcstory/metrics.js";
import source from "$lib/public/agentstory/AgentScrollStory.svelte?raw";
import styles from "$lib/public/agentstory/agentstory.css?raw";

// The ships page mounted by the shared route harness imports MapLibre's
// worker URL, which the Bazel test sandbox cannot resolve.
vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({
  default: "/test-worker.js",
}));

vi.mock(
  "$app/state",
  () => import("../../../../../test/profile-page.svelte.js"),
);
vi.mock("$app/stores", () => import("../../../../../test/profile-stores.js"));
let instance;
let target;
beforeEach(() => {
  // DOM contracts only. Real CSS and SSR/no-JS are checked by agents-preview.
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  target = document.createElement("div");
  document.body.append(target);
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  document.documentElement.removeAttribute("data-theme");
  document.body.style.overflow = "";
  localStorage.clear();
});
async function render(path = "/ember/agents", data = {}) {
  setPage(`https://jomcgi.dev${path}`, data);
  instance = mount(Route, { target, props: { surface: "agents", data } });
  await tick();
}

describe("agents technical-drawing production route", () => {
  it.each(["/ember/agents", "/public/ember/agents"])(
    "composes one chrome and preserves SEO at %s",
    async (path) => {
      await render(path);
      expect(target.querySelectorAll("main.td.sheet.agent-sheet")).toHaveLength(
        1,
      );
      expect(target.querySelectorAll(".chrome, button.scheme")).toHaveLength(2);
      expect(
        target.querySelector(".md-nav, .ember-site, .scroller"),
      ).toBeNull();
      expect(target.querySelectorAll("h1")).toHaveLength(1);
      expect(
        target.querySelector('.site-index [aria-current="page"]'),
      ).toBeNull();
      for (const current of target.querySelectorAll('[aria-current="page"]'))
        expect(current.textContent).toBe("agents");
      expect(target.querySelector("h1").textContent).toBe(
        "One microVM per agent session.",
      );
      expect(document.title).toBe(
        "One microVM per agent session · ember/agents",
      );
      expect(
        document.head.querySelector('meta[name="description"]').content,
      ).toBe(
        `How this site runs its AI agents: one Firecracker microVM per session, restored from a shared base snapshot in ${sessionRestoreMs} ms, killed 20 s after the last turn, rebuilt from a workspace tiered to S3. Credentials stay outside the VM.`,
      );
      for (const href of ["/", "/ember", "/ember/firecracker"])
        expect(target.querySelector(`a[href="${href}"]`)).not.toBeNull();
      for (const link of target.querySelectorAll('a[href^="#"]'))
        expect(target.querySelector(link.getAttribute("href"))).not.toBeNull();
    },
  );

  it("renders every phase, wire call and scheduled message in reading order", async () => {
    await render();
    const phases = [...target.querySelectorAll("[data-phase]")];
    expect(phases.map((el) => el.id)).toEqual(Object.keys(CALLS));
    for (const el of phases) {
      expect([Number(el.dataset.start), Number(el.dataset.end)]).toEqual(
        PHASES[el.id],
      );
      expect(
        [...el.querySelectorAll(".agent-calls code")].map(
          (item) => item.textContent,
        ),
      ).toEqual(CALLS[el.id].map((call) => call.text));
      expect(
        el.querySelector("figure[role=img]").getAttribute("aria-label").length,
      ).toBeGreaterThan(30);
    }
    expect(
      [...target.querySelectorAll("[data-at]")].map((el) =>
        Number(el.dataset.at),
      ),
    ).toEqual(CHAT_SCHEDULE.map(([at]) => at));
    expect(
      [...target.querySelectorAll("[data-at]")].map((el) => el.textContent),
    ).toEqual([
      "did last night's chart bump deploy?",
      `vm awake · ${sessionRestoreMs} ms`,
      "clone jomcgi/homelab · full history",
      "Checking ArgoCD and the chart versions.",
      "creds attached outside the vm",
      "Yes. The chart moved and the rollout is healthy.",
      "idle 20 s · vm destroyed · disk kept",
      "+2 days · workspace in s3",
      "add a regression test for that fix",
      "restored on new node · --resume",
      "Picking up where we left off.",
    ]);
    for (const text of [
      "no network card · one channel to the host",
      "Bearer <placeholder>",
      "Bearer ●●●●●●●● · real",
      "session-workspace/<lineage>",
      "deleted after 7 days",
      "~22 ms",
      "2.5 ms",
    ])
      expect(target.textContent).toContain(text);
  });

  it("preserves maintenance status, scheme persistence and the body's owner on cleanup", async () => {
    document.body.style.overflow = "visible";
    await render("/ember/agents", {
      maintenanceBanner: "Synthetic maintenance notice",
    });
    expect(target.querySelector('[role="status"]').textContent).toBe(
      "Synthetic maintenance notice",
    );
    target.querySelector("button.scheme").click();
    await tick();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("td-theme")).toBe("dark");
    expect(document.body.style.overflow).toBe("visible");
    await unmount(instance);
    instance = undefined;
    expect(document.body.style.overflow).toBe("visible");
  });

  it("has one static path without legacy paint, animation or viewport ownership", () => {
    expect(source + styles).not.toMatch(
      /--(?:em|ag)-|ember\.css|requestAnimationFrame|onMount|document\.body|#[0-9a-f]{3,8}\b|\bhsl\(/i,
    );
    expect(styles).not.toMatch(
      /overflow(?:-x|-y)?:\s*(?:hidden|clip)|position:\s*(?:fixed|sticky)|box-shadow|border-radius|gradient/,
    );
  });
});
