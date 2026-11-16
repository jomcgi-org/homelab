// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { createClassComponent } from "svelte/legacy";
import Page from "./+page.svelte";

const mounted = [];

async function render(data, mutable = false) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mutable
    ? createClassComponent({ component: Page, target, props: { data } })
    : mount(Page, { target, props: { data } });
  mounted.push({ component, target, mutable });
  await tick();
  return { component, target };
}

afterEach(async () => {
  for (const { component, target, mutable } of mounted.splice(0)) {
    if (mutable) component.$destroy();
    else await unmount(component);
    target.remove();
  }
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Codex login page", () => {
  test.each(["prod", "dev"])(
    "offers wait and explicit device recovery for blocked %s",
    async (environment) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () =>
          Response.json({
            user_code: "RECOVERY-CODE",
            verification_url: "https://example.test/device",
            environment,
            grant: "codex-cluster",
          }),
        ),
      );
      vi.stubGlobal("navigator", {});
      vi.spyOn(window, "open").mockImplementation(() => null);
      const { component, target } = await render(
        { environment, state: "blocked" },
        true,
      );

      expect(target.querySelector('[role="status"]').textContent).toBe(
        "Waiting for refreshed credentials",
      );
      expect(target.textContent).toContain("wait and check the");
      expect(target.textContent).toContain("new device login below");
      expect(target.querySelector("button.check").textContent).toBe(
        "Check connection",
      );
      expect(globalThis.fetch).not.toHaveBeenCalled();

      target.querySelector("button.primary-action").click();
      await vi.waitFor(() =>
        expect(target.querySelector("code")?.textContent).toBe("RECOVERY-CODE"),
      );
      expect(globalThis.fetch).toHaveBeenCalledWith(
        `/private/factory/execution/codex-login/start${environment === "dev" ? "?grant=codex-cluster&environment=dev" : ""}`,
        { method: "POST" },
      );

      component.$set({ data: { environment, state: "granted" } });
      await tick();
      expect(target.querySelector('[role="status"]').textContent).toBe(
        "Login saved",
      );
      expect(target.querySelector("code")).toBeNull();
      expect(target.querySelector("button.primary-action")).toBeNull();
      expect(globalThis.fetch).toHaveBeenCalledOnce();
    },
  );

  test.each(["prod", "dev"])(
    "retries saving an approved %s login without another device code",
    async (environment) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () =>
          Response.json(
            {
              state: "pending",
              persistence_retry: true,
              environment,
              grant: "codex-cluster",
            },
            { status: 202 },
          ),
        ),
      );
      const open = vi.spyOn(window, "open").mockImplementation(() => null);
      const { component, target } = await render(
        { environment, state: "persistence_pending" },
        true,
      );

      const retry = target.querySelector("button.primary-action");
      expect(retry.textContent.trim()).toBe("Retry saving login");
      expect(target.textContent).toContain("Approval is complete");
      retry.click();
      await vi.waitFor(() =>
        expect(target.textContent).toContain("Saving the approved login"),
      );
      expect(target.querySelector('[role="alert"]')).toBeNull();
      expect(target.querySelector("code")).toBeNull();
      expect(target.querySelector("button.primary-action").disabled).toBe(
        false,
      );
      expect(
        target.querySelector("button.primary-action").textContent.trim(),
      ).toBe("Check login status");
      expect(open).not.toHaveBeenCalled();
      expect(globalThis.fetch).toHaveBeenCalledOnce();

      component.$set({ data: { environment, state: "granted" } });
      await tick();
      expect(target.textContent).toContain("Login saved");
      expect(target.querySelector("button.primary-action")).toBeNull();
    },
  );

  test("offers an explicit dev action and a read-only connection check", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { target } = await render({ environment: "dev", state: "none" });

    expect(target.querySelector("h2").textContent).toBe("Dev conformance");
    expect(
      target.querySelector('nav a[aria-current="page"]').getAttribute("href"),
    ).toBe("?environment=dev");
    expect(
      target.querySelector("button.primary-action").textContent.trim(),
    ).toBe("Authorize Codex for dev conformance");
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(target.querySelector("form").getAttribute("method")).toBe("GET");
    expect(target.querySelector('input[name="environment"]').value).toBe("dev");
    expect(
      target.querySelector("form").hasAttribute("data-sveltekit-reload"),
    ).toBe(true);
    expect(
      [...target.querySelectorAll("nav a")].every((link) =>
        link.hasAttribute("data-sveltekit-reload"),
      ),
    ).toBe(true);
  });

  test.each(["granted", "unavailable"])(
    "does not offer a login start while status is %s",
    async (state) => {
      vi.stubGlobal("fetch", vi.fn());
      const { target } = await render({ environment: "dev", state });

      expect(target.querySelector("button.primary-action")).toBeNull();
      expect(target.querySelector("button.check").textContent).toBe(
        "Check connection",
      );
      expect(globalThis.fetch).not.toHaveBeenCalled();
    },
  );

  test("drops the dev code when navigation selects production", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          user_code: "DEV-CODE",
          verification_url: "https://example.test/device",
          environment: "dev",
          grant: "codex-cluster",
        }),
      ),
    );
    vi.stubGlobal("navigator", {});
    vi.spyOn(window, "open").mockImplementation(() => null);
    const { component, target } = await render(
      { environment: "dev", state: "none" },
      true,
    );

    target.querySelector("button.primary-action").click();
    await vi.waitFor(() =>
      expect(target.querySelector("code")?.textContent).toBe("DEV-CODE"),
    );
    component.$set({ data: { environment: "prod", state: "none" } });
    await tick();

    expect(target.querySelector("code")).toBeNull();
    expect(target.querySelector("h2").textContent).toBe("Production");
    expect(
      target.querySelector("button.primary-action").textContent.trim(),
    ).toBe("Authorize Codex for production");
    expect(globalThis.fetch).toHaveBeenCalledOnce();
  });
});
