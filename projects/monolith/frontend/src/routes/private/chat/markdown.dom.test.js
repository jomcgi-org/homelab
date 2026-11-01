// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { mount, unmount, tick } from "svelte";
import Page from "./+page.svelte";
import {
  attacks,
  chunks,
  createChatFixture,
  graphEvent,
  noteId,
} from "../../../../test/chat-fixtures.js";

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

async function settle() {
  // SSE reads, fetch.json and Svelte effects settle in separate microtasks.
  for (let i = 0; i < 8; i++) {
    await Promise.resolve();
    await tick();
  }
}

function expectInert(root) {
  expect(root).not.toBeNull();
  expect(root.querySelectorAll("img, script")).toHaveLength(0);
  for (const el of root.querySelectorAll("*")) {
    for (const attr of el.attributes) {
      expect(attr.name).not.toMatch(/^on/i);
      if (attr.name === "href")
        expect(attr.value).not.toMatch(/^\s*javascript:/i);
    }
  }
}

function expectMarkdown(root) {
  expectInert(root);
  for (const html of attacks.slice(0, 2))
    expect(root.textContent).toContain(html);
  // The shared renderer intentionally paints headings as p.md-h.
  expect(
    [...root.querySelectorAll("p.md-h")].map((el) => el.textContent),
  ).toEqual(["Synthetic heading", "Second heading", "Third heading"]);
  expect(root.querySelector("p.md-h.deep").textContent).toBe("Third heading");
  expect(
    root.querySelector('a[href="https://example.com/notes"]').textContent,
  ).toBe("Safe reference");
  expect(
    [...root.querySelectorAll("ul > li")].map((el) => el.textContent),
  ).toEqual(["Unordered first", "Unordered second"]);
  expect(root.querySelector("ol").getAttribute("start")).toBe("3");
  expect(root.querySelectorAll("ol > li")).toHaveLength(2);
  expect(root.querySelector('pre[data-lang="js"] code').textContent).toContain(
    'const text = "<img src=x>";',
  );
  expect(
    [...root.querySelectorAll("a")].some((el) => el.textContent === "x"),
  ).toBe(false);
}

it("keeps streamed assistant Markdown and graph-fetched drawer notes inert on the actual chat route", async () => {
  vi.useFakeTimers();
  const fixture = createChatFixture();
  vi.stubGlobal("fetch", vi.fn(fixture.fetch));
  instance = mount(Page, { target: document.body });
  await settle();

  const input = document.querySelector(".chat-input-bar input");
  const userText = `# Plain user text ${attacks.join(" ")}`;
  input.value = userText;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
  document.querySelector(".chat-input-bar button").click();
  await settle();
  expect(fixture.requests).toEqual([
    {
      url: "/private/chat",
      method: "POST",
      body: JSON.stringify({ message: userText, history: [] }),
    },
  ]);
  const user = document.querySelector(".chat-msg--user .chat-text");
  expect(user.textContent).toBe(userText);
  expect(user.children).toHaveLength(0);
  expectInert(user);
  expect(input.disabled).toBe(true);

  const assistant = document.querySelector(".chat-msg--assistant .chat-text");
  expect(assistant.tagName).toBe("DIV");
  for (const [index, text] of chunks.entries()) {
    fixture.emit({ type: "text_chunk", data: { text } });
    await settle();
    expectInert(assistant);
    expect(document.querySelector(".chat-cursor")).not.toBeNull();
    if (index >= 1) expect(assistant.textContent).toContain(attacks[0]);
    if (index >= 2) expect(assistant.textContent).toContain(attacks[1]);
    expect(user.textContent).toBe(userText);
  }
  expectMarkdown(assistant);

  fixture.emit(graphEvent);
  await settle();
  const graphNode = document.querySelector('.graph-hit-areas [role="button"]');
  expect(graphNode.getAttribute("aria-label")).toBe("Synthetic linked note");
  graphNode.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  await settle();
  expect(fixture.requests[1]).toEqual({
    url: `/private/chat?note_id=${encodeURIComponent(noteId)}`,
    method: "GET",
    body: undefined,
  });
  const drawer = document.querySelector(".drawer-content");
  expectMarkdown(drawer);
  expect(drawer.textContent).not.toContain("removed-frontmatter");
  expect(drawer.textContent).not.toContain("removed-trailing-link");
  expect(drawer.textContent).not.toContain("## Links");

  fixture.finish();
  await settle();
  expectMarkdown(assistant);
  expectMarkdown(drawer);
  expect(input.disabled).toBe(false);
  expect(document.querySelector(".chat-cursor")).toBeNull();
  document.querySelector(".drawer-close").click();
  await settle();
  expect(document.querySelector(".note-drawer")).toBeNull();
  expectInert(document.querySelector(".chat-log"));
});
