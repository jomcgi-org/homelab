// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import CodexLogin from "./CodexLogin.svelte";
import { RUN_LEXICON as P } from "./run-lexicon.js";

const mounted = [];

async function render(onError = vi.fn()) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(CodexLogin, {
    target,
    props: {
      authorizeLabel: P.labels.authorizeCodex,
      authorizingLabel: P.labels.authorizingCodex,
      copiedLabel: P.labels.copied,
      unavailableLabel: P.labels.codexLoginUnavailable,
      invalidResponseLabel: P.labels.codexLoginInvalidResponse,
      codeLabel: P.labels.codexLoginCode,
      openLinkLabel: P.labels.codexLoginOpenLink,
      onError,
    },
  });
  mounted.push({ component, target });
  await tick();
  return { target, onError };
}

afterEach(async () => {
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Codex authorization", () => {
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
