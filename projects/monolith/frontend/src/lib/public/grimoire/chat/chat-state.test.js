import { describe, it, expect } from "vitest";
import { freshChatState } from "./chat-state.js";

describe("freshChatState", () => {
  it("starts an empty transcript with no grounding and an idle turn", () => {
    const s = freshChatState();
    expect(s.messages).toEqual([]);
    expect(s.touchedMap.size).toBe(0);
    expect(s.turn.status).toBe("idle");
    expect(s.turn.assistant).toBe("");
    expect(s.notice).toBeNull();
    expect(s.input).toBe("");
    expect(s.lastUserMessage).toBe("");
  });

  it("returns fresh references each call (a reset cannot alias old state)", () => {
    const a = freshChatState();
    const b = freshChatState();
    a.messages.push({ role: "user", content: "hi" });
    a.touchedMap.set(1, { id: 1, title: "x" });
    expect(b.messages).toEqual([]);
    expect(b.touchedMap.size).toBe(0);
  });
});
