// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import HandoutComposer from "./HandoutComposer.svelte";

const campaignId = "11111111-1111-4111-8111-111111111111";
const pcA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const pcB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const characters = [
  { id: pcA, character_name: "Aria" },
  { id: pcB, character_name: "Bram" },
];

const mounted = [];
async function composer(send = vi.fn(async () => ({ id: "event" }))) {
  const target = document.createElement("div");
  document.body.append(target);
  const instance = mount(HandoutComposer, {
    target,
    props: { campaignId, characters, send },
  });
  mounted.push({ instance, target });
  await tick();
  return { root: target, send };
}

async function type(input, value) {
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
}

const submit = async (root) => {
  root
    .querySelector("form")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await tick();
  await tick();
};

afterEach(async () => {
  for (const { instance, target } of mounted.splice(0)) {
    await unmount(instance);
    target.remove();
  }
  vi.unstubAllGlobals();
});

describe("HandoutComposer", () => {
  it("previews the markdown live without script", async () => {
    const { root } = await composer();
    await type(root.querySelector("input[required]"), "Letter");
    await type(
      root.querySelector("textarea"),
      "Hello **you**<script>x()</script>",
    );
    const preview = root.querySelector(".preview");
    expect(preview.querySelector("h3").textContent).toBe("Letter");
    expect(preview.querySelector("strong").textContent).toBe("you");
    expect(preview.querySelector("script")).toBeNull();
  });

  it("will not send without a title", async () => {
    const { root } = await composer();
    expect(root.querySelector("form > button").disabled).toBe(true);
  });

  it("sends a table handout through the composer payload", async () => {
    const { root, send } = await composer();
    await type(root.querySelector("input[required]"), " Map ");
    await type(root.querySelector("textarea"), "Text");
    await submit(root);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toMatchObject({
      operation: "handout",
      title: "Map",
      markdown: "Text",
      audience: "table",
      pcIds: [],
    });
    expect(send.mock.calls[0][0].requestId).toBeTruthy();
    // Accepted: the draft clears.
    expect(root.querySelector("input[required]").value).toBe("");
  });

  it("sends to the picked players only", async () => {
    const { root, send } = await composer();
    await type(root.querySelector("input[required]"), "Secret");
    const everyone = root.querySelector('fieldset input[type="checkbox"]');
    everyone.click();
    await tick();
    const boxes = [...root.querySelectorAll("fieldset input[type=checkbox]")];
    expect(boxes).toHaveLength(3);
    boxes[2].click();
    await tick();
    await submit(root);
    expect(send.mock.calls[0][0]).toMatchObject({
      audience: "pcs",
      pcIds: [pcB],
    });
  });

  it("keeps the draft and reuses the request id when the send fails", async () => {
    const send = vi.fn(async () => undefined);
    const { root } = await composer(send);
    await type(root.querySelector("input[required]"), "Map");
    await submit(root);
    await submit(root);
    expect(root.querySelector("input[required]").value).toBe("Map");
    expect(send.mock.calls[1][0].requestId).toBe(
      send.mock.calls[0][0].requestId,
    );
  });

  it("uploads an image through the proxy and sends its reference", async () => {
    const fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            key: "campaigns/x/handouts/y.png",
            content_type: "image/png",
            size: 2048,
          }),
          { status: 201, headers: { "content-type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetch);
    const { root, send } = await composer();
    await type(root.querySelector("input[required]"), "Map");
    const input = root.querySelector('input[type="file"]');
    Object.defineProperty(input, "files", {
      value: [new File([new Uint8Array(4)], "pass.png", { type: "image/png" })],
    });
    input.dispatchEvent(new Event("change", { bubbles: true }));
    for (let i = 0; i < 6; i += 1) await tick();
    expect(fetch.mock.calls[0][0]).toBe(
      `/grimoire/campaigns/${campaignId}/handouts/uploads`,
    );
    expect(fetch.mock.calls[0][1].body.get("file").size).toBe(4);
    expect(root.textContent).toContain("Uploaded pass.png (image/png, 2 KB)");
    await submit(root);
    expect(send.mock.calls[0][0].image).toEqual({
      source: "upload",
      key: "campaigns/x/handouts/y.png",
    });
  });

  it("shows the backend refusal when an upload fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: "unsupported image type" }), {
            status: 415,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    const { root } = await composer();
    const input = root.querySelector('input[type="file"]');
    Object.defineProperty(input, "files", {
      value: [new File(["x"], "a.txt", { type: "text/plain" })],
    });
    input.dispatchEvent(new Event("change", { bubbles: true }));
    for (let i = 0; i < 6; i += 1) await tick();
    expect(root.querySelector('[role="alert"]').textContent).toBe(
      "unsupported image type",
    );
  });
});
