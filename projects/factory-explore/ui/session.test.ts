// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { createApp } from "vue";
import { User, UserManager } from "oidc-client-ts";

vi.mock("vuetify/components", () =>
  Object.fromEntries(
    [
      "VApp",
      "VAppBar",
      "VAppBarTitle",
      "VBtn",
      "VContainer",
      "VDataTable",
      "VMain",
      "VSelect",
      "VTextField",
    ].map((name) => [name, { template: "<div><slot /></div>" }]),
  ),
);

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  vi.resetModules();
});

test.each([
  "deadline",
  "expired token",
  "expiry while suspended",
  "window denial",
])("%s ends the explorer", async (reason) => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-01-01T12:00:00Z"));
  window.__base_url = "/";
  const redirect = vi
    .spyOn(UserManager.prototype, "signinRedirect")
    .mockResolvedValue();
  const silent = vi
    .spyOn(UserManager.prototype, "signinSilent")
    .mockResolvedValue(null);
  const { userManager } = await import("./src/apiConfig");
  const lifetime =
    reason === "expired token"
      ? -1
      : reason === "expiry while suspended"
        ? 1
        : 600;
  const exp = Date.now() / 1000 + lifetime;
  await userManager.storeUser(
    new User({
      id_token: "test-token",
      access_token: "access-token",
      token_type: "Bearer",
      profile: { exp },
      expires_at: exp,
    }),
  );
  const denied = reason === "window denial";
  const fetchApi = vi.fn(
    async () =>
      new Response(
        JSON.stringify({ kind: "APIGroupDiscoveryList", items: [] }),
        {
          status: denied ? 403 : 200,
          headers: denied
            ? { "X-Explore-Next-Window": "2026-01-01T13:00:00Z" }
            : { "X-Explore-Window-End": "2026-01-01T12:00:02Z" },
        },
      ),
  );
  vi.stubGlobal("fetch", fetchApi);
  const app = createApp((await import("./src/OIDCApp.vue")).default);
  const root = document.createElement("div");
  app.mount(root);
  await vi.advanceTimersByTimeAsync(0);
  if (reason === "deadline") {
    expect(root.textContent).toContain("0:02 left");
    await vi.advanceTimersByTimeAsync(2000);
  }
  if (reason === "expiry while suspended") {
    expect(root.textContent).toContain("0:02 left");
    vi.setSystemTime(new Date("2026-01-01T12:00:01Z"));
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
  }
  expect(root.textContent).toContain("Time’s up");
  if (reason !== "expired token") {
    const opens = denied ? "2026-01-01T13:00:00Z" : "2026-01-01T12:50:02Z";
    expect(root.textContent).toContain(new Date(opens).toLocaleString());
  }
  expect(await userManager.getUser()).toBeNull();
  expect(JSON.stringify(sessionStorage)).not.toContain("test-token");
  app.unmount();
  vi.resetModules();
  const reopened = createApp((await import("./src/OIDCApp.vue")).default);
  reopened.mount(root);
  await vi.advanceTimersByTimeAsync(0);
  expect(root.textContent).toContain("Time’s up");
  expect(redirect).not.toHaveBeenCalled();
  expect(silent).not.toHaveBeenCalled();
  root.querySelector("button")!.click();
  expect(redirect).toHaveBeenCalledTimes(1);
  reopened.unmount();
});

test("a rejected token leaves the last authenticated deadline intact", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-01-01T12:00:00Z"));
  const { read, userManager, countdown, nextWindow } =
    await import("./src/apiConfig");
  const exp = Date.now() / 1000 + 600;
  vi.spyOn(userManager, "getUser").mockResolvedValue(
    new User({
      id_token: "rejected-token",
      access_token: "access-token",
      token_type: "Bearer",
      profile: { exp },
      expires_at: exp,
    }),
  );
  const fetchApi = vi
    .fn()
    .mockResolvedValueOnce(
      new Response("{}", {
        headers: { "X-Explore-Window-End": "2026-01-01T12:05:00Z" },
      }),
    )
    .mockResolvedValueOnce(
      new Response(null, {
        status: 401,
        headers: { "X-Explore-Window-End": new Date().toISOString() },
      }),
    );
  vi.stubGlobal("fetch", fetchApi);
  await read("/api", "application/json");
  await expect(read("/apis", "application/json")).rejects.toThrow(
    "401 Unauthorized",
  );
  expect(countdown.value).toBe("5:00");
  expect(nextWindow.value).toBeNull();
  expect(sessionStorage.getItem("explore.windowEnd")).toBe(
    String(Date.parse("2026-01-01T12:05:00Z")),
  );
});
