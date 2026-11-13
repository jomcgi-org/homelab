// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { get } from "svelte/store";
import Harness from "./ExecutionHarness.svelte";
import { createExecutionFixture, NOW, session } from "./execution-fixtures.js";
import { executionLocation, page } from "./execution-stores.js";
import { goto } from "$app/navigation";

vi.mock("$app/navigation", () => ({ goto: vi.fn() }));

const origin = "https://private.fixture.invalid";
const notice = "Synthetic login required to continue this session";
const userCode = "FIXT-7806";
const verificationUrl = "https://login.fixture.invalid/device";
const login = {
  login_required: true,
  verification_url: verificationUrl,
  user_code: userCode,
};
let component;
let target;
let fixture;
let requests;
let originalPage;
let originalStorage;

// Flush promises and Svelte work without advancing any polling clock.
async function settle() {
  for (let n = 0; n < 40; n++) await tick();
}
const response = (body) =>
  new Response(JSON.stringify(body), {
    headers: { "content-type": "application/json" },
  });

function installScenario(kind) {
  let paused = false;
  const id = kind === "message" ? session.id : 97806;
  const postPath =
    kind === "message"
      ? `/factory/execution/session/${id}/messages`
      : "/api/swarm/classify-and-start";
  const payload =
    kind === "message"
      ? { prompt: "Continue synthetic work", model: "luna" }
      : {
          task: "Start synthetic work",
          model: "luna",
          repo: null,
          branch: null,
        };
  const pausedSession = {
    ...session,
    id,
    status: "awaiting_login",
    title: "Synthetic session awaiting authentication",
  };
  vi.stubGlobal("fetch", async (input, options = {}) => {
    const url = new URL(String(input), origin);
    const method = options.method || "GET";
    requests.push({ url: url.href, method, body: options.body });
    if (url.origin === origin) {
      if (method === "POST" && url.pathname === postPath && !url.search) {
        expect(paused, "only one recovery POST").toBe(false);
        expect(new Headers(options.headers).get("content-type")).toBe(
          "application/json",
        );
        expect(JSON.parse(options.body)).toEqual(payload);
        paused = true;
        return response(
          kind === "message"
            ? { ...login, accepted: false, message: notice }
            : {
                ...login,
                kind: "session",
                session_id: id,
                login_message: notice,
              },
        );
      }
      if (
        method === "GET" &&
        url.pathname === "/factory/execution/data" &&
        !url.search
      ) {
        return response({ sessions: paused ? [pausedSession] : [session] });
      }
      if (
        paused &&
        method === "GET" &&
        url.pathname === `/factory/execution/session/${id}`
      ) {
        expect(url.search).toBe(kind === "message" ? "?after_seq=1" : "");
        return response({
          session: pausedSession,
          pending_queue: [],
          turns: [
            {
              seq: 2,
              prompt: "Synthetic login checkpoint",
              model: "luna",
              created_at: NOW,
              result_text: "Synthetic detail refreshed after login response",
              terminal_reason: "completed",
            },
          ],
        });
      }
    }
    // Preserve the shared fixture's refusal of external traffic and all other POSTs.
    return fixture.fetch(input, options);
  });
  return { id, postPath, payload };
}

beforeEach(() => {
  originalPage = get(page);
  originalStorage = Object.entries(localStorage);
  localStorage.clear();
  vi.useFakeTimers();
  vi.setSystemTime(new Date(NOW));
  requests = [];
  fixture = createExecutionFixture();
  vi.stubGlobal("EventSource", fixture.EventSource);
  vi.mocked(goto).mockImplementation(async (href) => {
    page.update((value) => ({ ...value, url: new URL(href, value.url) }));
  });
  target = document.createElement("div");
  document.body.append(target);
});

afterEach(async () => {
  try {
    if (component) await unmount(component);
    component = null;
    await settle();
    expect(
      fixture.stats.created,
      "real fixture stream mounted",
    ).toBeGreaterThan(0);
    expect(fixture.stats.active, "unmount closes every stream").toBe(0);
    expect(fixture.stats.closed).toBe(fixture.stats.created);
    expect(vi.getTimerCount(), "unmount clears every timer").toBe(0);
    const count = requests.length;
    await vi.advanceTimersByTimeAsync(60000);
    await settle();
    expect(requests, "no fetch after unmount").toHaveLength(count);
    expect(fixture.stats.unexpected).toEqual([]);
  } finally {
    target.remove();
    page.set(originalPage);
    localStorage.clear();
    for (const [key, value] of originalStorage)
      localStorage.setItem(key, value);
    vi.unstubAllGlobals();
    vi.resetAllMocks();
    vi.restoreAllMocks();
    vi.useRealTimers();
  }
});

async function submit(formSelector, prompt) {
  const form = target.querySelector(formSelector);
  expect(form).not.toBeNull();
  const picker = form.querySelector("select");
  picker.value = "luna";
  picker.dispatchEvent(new Event("change"));
  const composer = form.querySelector("textarea");
  composer.value = `  ${prompt}  `;
  // Update the real bound control locally; window input also triggers prewarming.
  composer.dispatchEvent(new Event("input"));
  await settle();
  expect(form.querySelector('button[type="submit"]').disabled).toBe(false);
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await settle();
}

function expectRecoveryUI() {
  expect(target.textContent).toContain(
    "Synthetic session awaiting authentication",
  );
  expect(target.textContent).toContain(
    "Synthetic detail refreshed after login response",
  );
  expect(target.querySelector(".awaiting_login")).not.toBeNull();
  expect(target.textContent).toContain(notice);
  expect(target.querySelector("#codex-device-code")?.textContent).toBe(
    userCode,
  );
  expect(
    target.querySelector(".codex-login-control a")?.getAttribute("href"),
  ).toBe(verificationUrl);
  expect(target.textContent).not.toContain("Message was not accepted");
  expect(fixture.stats.unexpected).toEqual([]);
}

function expectPostAndRefresh(baseline, scenario, detailSearch) {
  const subsequent = requests.slice(baseline);
  expect(subsequent.filter(({ method }) => method === "POST")).toEqual([
    {
      url: origin + scenario.postPath,
      method: "POST",
      body: JSON.stringify(scenario.payload),
    },
  ]);
  expect(
    subsequent.filter(
      ({ method, url }) =>
        method === "GET" && url === `${origin}/factory/execution/data`,
    ),
    "immediate session list refresh before polling",
  ).toHaveLength(1);
  expect(
    subsequent.filter(
      ({ method, url }) =>
        method === "GET" &&
        url ===
          `${origin}/factory/execution/session/${scenario.id}${detailSearch}`,
    ),
    "immediate selected detail refresh before polling",
  ).toHaveLength(1);
  expect(Date.now()).toBe(new Date(NOW).getTime());
}

test("message login recovery immediately refreshes sessions and detail with polling held back", async () => {
  const scenario = installScenario("message");
  executionLocation("execution", "?session=901");
  component = mount(Harness, { target });
  await settle();
  expect(target.textContent).toContain("Synthetic running session");
  expect(target.textContent).toContain("Synthetic transcript update 0");
  expect(
    requests.some(
      ({ url }) => url === `${origin}/factory/execution/session/901`,
    ),
  ).toBe(true);
  const baseline = requests.length;
  await submit("form.composer", "Continue synthetic work");
  expectPostAndRefresh(baseline, scenario, "?after_seq=1");
  expectRecoveryUI();
});

test("launcher login recovery refreshes the list and selects the returned session", async () => {
  const scenario = installScenario("start");
  executionLocation("execution");
  component = mount(Harness, { target });
  await settle();
  // Desktop arrival selects the active fixture run. Return to the launcher.
  executionLocation("execution");
  await settle();
  vi.mocked(goto).mockClear();
  expect(target.querySelector("form.launcher-form")).not.toBeNull();
  const baseline = requests.length;
  await submit("form.launcher-form", "Start synthetic work");
  expectPostAndRefresh(baseline, scenario, "");
  expect(goto).toHaveBeenCalledExactlyOnceWith(
    `/factory/execution?session=${scenario.id}`,
    { noScroll: true, keepFocus: true },
  );
  expect(get(page).url.searchParams.get("session")).toBe(String(scenario.id));
  expectRecoveryUI();
});
