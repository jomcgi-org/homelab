import { describe, expect, it } from "vitest";
import { composeHandout, handoutEvent, handoutImageUrl } from "./handout.js";

const pcA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const pcB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const entity = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const upload = {
  source: "upload",
  key: `campaigns/${pcA}/handouts/${"f".repeat(32)}.png`,
};

describe("composeHandout", () => {
  it("addresses the table with no PC list and no optional fields", () => {
    expect(
      composeHandout({
        title: "  Map of the pass  ",
        markdown: "Mind the ice.",
        audience: "table",
      }),
    ).toEqual({
      operation: "handout",
      title: "Map of the pass",
      markdown: "Mind the ice.",
      audience: "table",
      pcIds: [],
    });
  });

  it("turns one chosen player into a pcs audience", () => {
    const message = composeHandout({
      title: "Letter",
      markdown: "For your eyes.",
      audience: [`pc:${pcA}`],
    });
    expect(message.audience).toBe("pcs");
    expect(message.pcIds).toEqual([pcA]);
  });

  it("names several players and carries the entity and image", () => {
    const message = composeHandout({
      title: "Letter",
      markdown: "",
      audience: [`pc:${pcA}`, `pc:${pcB}`],
      entityId: entity,
      image: upload,
    });
    expect(message).toMatchObject({
      audience: "pcs",
      pcIds: [pcA, pcB],
      entityId: entity,
      image: upload,
    });
  });

  it("omits entity and image when none were chosen", () => {
    const message = composeHandout({
      title: "Letter",
      markdown: "x",
      audience: "table",
      entityId: "",
      image: null,
    });
    expect("entityId" in message).toBe(false);
    expect("image" in message).toBe(false);
  });

  it("feeds the backend event the state endpoint builds from it", () => {
    const event = handoutEvent({
      ...composeHandout({
        title: " Letter ",
        markdown: "Body",
        audience: [`pc:${pcB}`],
        image: upload,
      }),
      requestId: "r-1",
    });
    expect(event).toEqual({
      kind: "handout",
      audience: "pcs",
      audience_pc_ids: [pcB],
      body: { title: "Letter", markdown: "Body", image: upload },
      request_id: "r-1",
    });
  });
});

describe("handoutImageUrl", () => {
  it("points at the members-only proxy, never a storage key", () => {
    const url = handoutImageUrl("c1", "s1", "e1");
    expect(url).toBe("/grimoire/campaigns/c1/session/s1/events/e1/image");
    expect(url).not.toContain(upload.key);
  });

  it("encodes each id as one path segment", () => {
    expect(handoutImageUrl("a/b", "c?d", "e#f")).toBe(
      "/grimoire/campaigns/a%2Fb/session/c%3Fd/events/e%23f/image",
    );
  });
});
