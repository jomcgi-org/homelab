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

test.each(["deadline", "expired token", "window denial"])(
  "%s ends the explorer",
  async (reason) => {
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
    const exp = Date.now() / 1000 + (reason === "expired token" ? -1 : 600);
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
    expect(root.textContent).toContain("Time’s up");
    if (reason !== "expired token") {
      const opens =
        reason === "deadline" ? "2026-01-01T12:50:02Z" : "2026-01-01T13:00:00Z";
      expect(root.textContent).toContain(new Date(opens).toLocaleString());
    }
    expect(await userManager.getUser()).toBeNull();
    expect(JSON.stringify(sessionStorage)).not.toContain("test-token");
    expect(redirect).not.toHaveBeenCalled();
    expect(silent).not.toHaveBeenCalled();
    root.querySelector("button")!.click();
    expect(redirect).toHaveBeenCalledTimes(1);
    app.unmount();
  },
);
