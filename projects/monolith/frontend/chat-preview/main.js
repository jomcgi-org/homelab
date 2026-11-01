import "@homelab/design-system/tokens/contract.css";
import "../src/lib/global.css";
import "../src/lib/public/styles/technical-drawing.css";
import { mount, tick, unmount } from "svelte";
import Page from "../src/routes/private/chat/+page.svelte";
import {
  chunks,
  createChatFixture,
  graphEvent,
} from "../test/chat-fixtures.js";

// In-memory SSE and note responses only. No model, API or private data access.
const originalFetch = window.fetch;
const fixture = createChatFixture();
window.fetch = fixture.fetch;
const instance = mount(Page, { target: document.getElementById("app") });
window.__chatFixture = {
  requests: fixture.requests,
  async stream() {
    for (const text of chunks) {
      fixture.emit({ type: "text_chunk", data: { text } });
      await new Promise((resolve) => setTimeout(resolve, 100));
      await tick();
    }
    fixture.emit(graphEvent);
    fixture.finish();
    await tick();
  },
  async cleanup() {
    await unmount(instance);
    window.fetch = originalFetch;
  },
};
