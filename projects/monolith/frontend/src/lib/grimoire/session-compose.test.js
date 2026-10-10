import { describe, expect, it } from "vitest";
import { composeMessage } from "./session-compose.js";

const pcA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

describe("composeMessage", () => {
  it("addresses the table with no PC list", () => {
    expect(
      composeMessage({
        text: "The lantern flickers.",
        dm: true,
        audience: "table",
        replyTo: null,
        resolved: true,
      }),
    ).toEqual({
      operation: "post",
      text: "The lantern flickers.",
      kind: "narration",
      audience: "table",
      pcIds: [],
      replyTo: undefined,
      resolved: true,
    });
  });

  it("keeps a DM note on the DM audience", () => {
    const message = composeMessage({
      text: "Trap on the stairs.",
      dm: true,
      audience: "dm",
      replyTo: null,
      resolved: true,
    });
    expect(message.audience).toBe("dm");
    expect(message.pcIds).toEqual([]);
    expect(message.kind).toBe("narration");
  });

  it("turns a chosen PC into a pcs audience naming only that character", () => {
    const message = composeMessage({
      text: "You alone notice the mark.",
      dm: true,
      audience: `pc:${pcA}`,
      replyTo: null,
      resolved: true,
    });
    expect(message.audience).toBe("pcs");
    expect(message.pcIds).toEqual([pcA]);
    expect(message.text).toBe("You alone notice the mark.");
  });

  it("threads a private reply to the action it answers", () => {
    const message = composeMessage({
      text: "The door gives way.",
      dm: true,
      audience: `pc:${pcA}`,
      replyTo: { id: "action-event", name: "Aria" },
      resolved: false,
    });
    expect(message).toMatchObject({
      audience: "pcs",
      pcIds: [pcA],
      replyTo: "action-event",
      resolved: false,
    });
  });

  it("sends a player's words as an action, privately when asked", () => {
    expect(
      composeMessage({
        text: "I pocket the key.",
        dm: false,
        audience: "dm",
        replyTo: null,
        resolved: true,
      }),
    ).toMatchObject({ kind: "action", audience: "dm", pcIds: [] });
    expect(
      composeMessage({
        text: "I approach the door.",
        dm: false,
        audience: "table",
        replyTo: null,
        resolved: true,
      }),
    ).toMatchObject({ kind: "action", audience: "table", pcIds: [] });
  });
});
