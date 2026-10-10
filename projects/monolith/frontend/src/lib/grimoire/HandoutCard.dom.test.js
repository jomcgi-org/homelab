// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import HandoutCard from "./HandoutCard.svelte";
import { handoutImageUrl } from "./handout.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const sessionId = "33333333-3333-4333-8333-333333333333";
const eventId = "55555555-5555-4555-8555-555555555555";
const storageKey = `campaigns/${campaignId}/handouts/${"a".repeat(32)}.png`;

const handout = (overrides = {}) => ({
  id: eventId,
  kind: "handout",
  audience: "table",
  retracted_at: null,
  body: {
    title: "A map of the pass",
    markdown: "Mind the **ice**.\n\n<script>alert(1)</script>",
    image: { source: "upload", key: storageKey },
  },
  ...overrides,
});

const mounted = [];
async function card(props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const instance = mount(HandoutCard, {
    target,
    props: {
      handout: handout(),
      campaignId,
      sessionId,
      dm: false,
      pin: vi.fn(),
      ...props,
    },
  });
  mounted.push({ instance, target });
  await tick();
  return target;
}

afterEach(async () => {
  for (const { instance, target } of mounted.splice(0)) {
    await unmount(instance);
    target.remove();
  }
});

describe("HandoutCard", () => {
  it("renders the title and markdown", async () => {
    const root = await card();
    expect(root.querySelector("h3").textContent).toBe("A map of the pass");
    expect(root.querySelector("strong").textContent).toBe("ice");
  });

  it("never lets a script tag into the DOM", async () => {
    const root = await card();
    expect(root.querySelector("script")).toBeNull();
    expect(root.textContent).toContain("<script>alert(1)</script>");
  });

  it("serves the image through the proxy path, never the storage key", async () => {
    const root = await card();
    const image = root.querySelector("img");
    expect(image.getAttribute("src")).toBe(
      handoutImageUrl(campaignId, sessionId, eventId),
    );
    expect(image.getAttribute("alt")).toContain("A map of the pass");
    expect(root.innerHTML).not.toContain(storageKey);
  });

  it("renders no image when the handout has none", async () => {
    const root = await card({
      handout: handout({ body: { title: "Note", markdown: "Text" } }),
    });
    expect(root.querySelector("img")).toBeNull();
  });

  it("offers a player the pin action and pins that handout", async () => {
    const pin = vi.fn();
    const root = await card({ pin });
    const button = root.querySelector("button");
    expect(button.textContent).toBe("Pin to my notes");
    button.click();
    expect(pin).toHaveBeenCalledTimes(1);
    expect(pin.mock.calls[0][0].id).toBe(eventId);
  });

  it("offers the DM no pin action", async () => {
    const root = await card({ dm: true });
    expect(root.querySelector("button")).toBeNull();
    expect(root.querySelector("h3").textContent).toBe("A map of the pass");
  });

  it("renders a retracted handout without its body, image or pin", async () => {
    const root = await card({
      handout: handout({ retracted_at: "2026-10-10T12:05:00Z" }),
    });
    expect(root.textContent).toContain("This handout was retracted.");
    expect(root.textContent).not.toContain("A map of the pass");
    expect(root.textContent).not.toContain("ice");
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector("button")).toBeNull();
  });
});
