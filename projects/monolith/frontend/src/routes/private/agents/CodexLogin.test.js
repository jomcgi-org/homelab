// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { createClassComponent } from "svelte/legacy";
import CodexLogin from "./CodexLogin.svelte";
import { RUN_LEXICON as P } from "./run-lexicon.js";

const mounted = [];

async function render(onError = vi.fn(), props = {}, mutable = false) {
  const target = document.createElement("div");
  document.body.append(target);
  const componentProps = {
    authorizeLabel: P.labels.authorizeCodex,
    authorizingLabel: P.labels.authorizingCodex,
    copiedLabel: P.labels.copied,
    unavailableLabel: P.labels.codexLoginUnavailable,
    invalidResponseLabel: P.labels.codexLoginInvalidResponse,
    codeLabel: P.labels.codexLoginCode,
    openLinkLabel: P.labels.codexLoginOpenLink,
    requestNewCodeLabel: P.labels.codexLoginRequestNewCode,
    startingLabel: P.labels.codexLoginStarting,
    onError,
    ...props,
  };
  const component = mutable
    ? createClassComponent({
        component: CodexLogin,
        target,
        props: componentProps,
      })
    : mount(CodexLogin, { target, props: componentProps });
  mounted.push({ component, target, mutable });
  await tick();
  return { component, target, onError };
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

describe("Codex authorization", () => {
  test("renders authorize as the only control before a code exists", async () => {
    const { target } = await render();

    const controls = target.querySelectorAll("a, button");
    expect(controls).toHaveLength(1);
    expect(controls[0].tagName).toBe("BUTTON");
    expect(controls[0].classList.contains("primary-action")).toBe(true);
    expect(controls[0].textContent.trim()).toBe(P.labels.authorizeCodex);
    expect(target.querySelector("code")).toBeNull();
  });

  test("makes the login link primary and requesting a new code secondary", async () => {
    const { target } = await render(vi.fn(), {
      initialUserCode: "READY-123",
      initialVerificationUrl: "https://example.test/device",
    });

    const link = target.querySelector("a.primary-action");
    const button = target.querySelector("button.secondary-action");
    expect(link?.textContent.trim()).toBe(P.labels.codexLoginOpenLink);
    expect(link?.getAttribute("href")).toBe("https://example.test/device");
    expect(target.querySelector("code")?.textContent).toBe("READY-123");
    expect(button?.textContent.trim()).toBe(P.labels.codexLoginRequestNewCode);
    expect(target.querySelectorAll("a, button")).toHaveLength(2);
    expect(target.querySelector("code")?.nextElementSibling).toBe(link);
    expect(link?.getAttribute("aria-describedby")).toBe("codex-device-code");
    expect(target.querySelector("code")?.hasAttribute("aria-label")).toBe(
      false,
    );
  });

  test("posts once, copies the code, and opens the verification URL", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            verification_url: "https://example.test/device",
            user_code: "CODE-123",
            expires_in: 900,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const { target } = await render();
    const button = target.querySelector("button");

    button.click();
    button.click();

    await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(1));
    await vi.waitFor(() =>
      expect(target.querySelector("code")?.textContent).toBe("CODE-123"),
    );
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/private/agents/codex-login/start",
      { method: "POST" },
    );
    expect(writeText).toHaveBeenCalledWith("CODE-123");
    expect(open).toHaveBeenCalledWith(
      "https://example.test/device",
      "_blank",
      "noopener,noreferrer",
    );
  });

  test("keeps the selectable code visible when clipboard access fails", async () => {
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: vi.fn(async () => Promise.reject(new Error("blocked"))),
      },
    });
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            verification_url: "https://example.test/device",
            user_code: "FALL-BACK",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const { target } = await render();

    target.querySelector("button").click();

    await vi.waitFor(() =>
      expect(target.querySelector("code")?.textContent).toBe("FALL-BACK"),
    );
    expect(open).toHaveBeenCalledTimes(1);
    expect(target.querySelector("code").getAttribute("tabindex")).toBe("0");
  });

  test("surfaces endpoint failures and re-enables the button", async () => {
    vi.stubGlobal("navigator", {});
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: "broker unavailable" }), {
          status: 502,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.spyOn(window, "open").mockImplementation(() => null);
    const { target, onError } = await render();

    target.querySelector("button").click();

    await vi.waitFor(() =>
      expect(onError).toHaveBeenLastCalledWith("broker unavailable"),
    );
    expect(target.querySelector("button").disabled).toBe(false);
    expect(target.querySelector("code")).toBeNull();
  });

  test("surfaces device flow startup as a retriable state", async () => {
    vi.stubGlobal("navigator", {});
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            retry_after: 10,
            message: "Device flow is starting, try again shortly.",
            pending: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.spyOn(window, "open").mockImplementation(() => null);
    const { target, onError } = await render();

    target.querySelector("button").click();

    await vi.waitFor(() =>
      expect(onError).toHaveBeenLastCalledWith(P.labels.codexLoginStarting),
    );
    expect(target.querySelector("button").disabled).toBe(false);
    expect(target.querySelector("code")).toBeNull();
  });

  test("includes the HTTP status when an error response has no message", async () => {
    vi.stubGlobal("navigator", {});
    globalThis.fetch = vi.fn(async () => new Response("", { status: 503 }));
    vi.spyOn(window, "open").mockImplementation(() => null);
    const { target, onError } = await render();

    target.querySelector("button").click();

    await vi.waitFor(() =>
      expect(onError).toHaveBeenLastCalledWith(
        `${P.labels.codexLoginUnavailable} (HTTP 503)`,
      ),
    );
    expect(target.querySelector("button").disabled).toBe(false);
  });

  test("accepts an identical pending code without reopening the login page", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            verification_url: "https://example.test/device",
            user_code: "PENDING-123",
            expires_in: 600,
            pending: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const onError = vi.fn();
    const { target } = await render(onError, {
      initialUserCode: "PENDING-123",
      initialVerificationUrl: "https://example.test/device",
    });

    target.querySelector("button.secondary-action").click();

    await vi.waitFor(() =>
      expect(writeText).toHaveBeenCalledWith("PENDING-123"),
    );
    expect(onError).toHaveBeenLastCalledWith(null);
    expect(open).not.toHaveBeenCalled();
    expect(target.querySelector("code")?.textContent).toBe("PENDING-123");
  });

  test("opens a new pending flow when no prior code exists", async () => {
    vi.stubGlobal("navigator", {});
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            verification_url: "https://example.test/device",
            user_code: "NEW-CODE",
            expires_in: 600,
            pending: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const { target } = await render();

    target.querySelector("button.primary-action").click();

    await vi.waitFor(() =>
      expect(target.querySelector("code")?.textContent).toBe("NEW-CODE"),
    );
    expect(open).toHaveBeenCalledWith(
      "https://example.test/device",
      "_blank",
      "noopener,noreferrer",
    );
  });

  test("replaces stale code when initialUserCode prop changes", async () => {
    vi.stubGlobal("navigator", {});
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            verification_url: "https://example.test/requested",
            user_code: "REQUESTED-X",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.spyOn(window, "open").mockImplementation(() => null);
    const { component, target } = await render(
      vi.fn(),
      {
        initialUserCode: "INITIAL-X",
        initialVerificationUrl: "https://example.test/initial",
      },
      true,
    );

    target.querySelector("button.secondary-action").click();
    await vi.waitFor(() =>
      expect(target.querySelector("code")?.textContent).toBe("REQUESTED-X"),
    );

    component.$set({
      initialUserCode: "INITIAL-Y",
      initialVerificationUrl: "https://example.test/next",
    });
    await tick();

    expect(target.querySelector("code")?.textContent).toBe("INITIAL-Y");
    expect(target.querySelector("a")?.getAttribute("href")).toBe(
      "https://example.test/next",
    );
  });

  test("renders the verification URL as a link when the popup is blocked", async () => {
    // window.open runs after an awaited fetch, so the user gesture is gone and a
    // popup blocker can swallow it. The link is the only way back to the page.
    vi.stubGlobal("navigator", {
      clipboard: { writeText: vi.fn(async () => {}) },
    });
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            verification_url: "https://example.test/device",
            user_code: "LINK-123",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.spyOn(window, "open").mockImplementation(() => null);
    const { target } = await render();

    target.querySelector("button").click();

    await vi.waitFor(() => expect(target.querySelector("a")).toBeTruthy());
    const link = target.querySelector("a");
    expect(link.getAttribute("href")).toBe("https://example.test/device");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.textContent.trim()).toBe(P.labels.codexLoginOpenLink);
  });
});
