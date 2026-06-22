import { describe, it, expect } from "vitest";
import { readHikeParams, writeHikeParams } from "./urlParams.js";

const VALID_NEAR = new Set(["__me__", "fort-william", "cairngorms"]);
const WALK_UUID = "e4040ab6-3bff-585c-b7cc-37bee42d4999";

function params(query) {
  return new URLSearchParams(query);
}

describe("readHikeParams", () => {
  it("defaults everything when no params are present", () => {
    expect(readHikeParams(params(""), VALID_NEAR)).toEqual({
      selectedDay: null,
      selectedWalk: null,
      nearKey: "",
      minDuration: "",
      maxDuration: "",
      minDistance: "",
      maxDistance: "",
      maxAscent: "",
    });
  });

  it("reads a full set of valid params", () => {
    const got = readHikeParams(
      params(
        `day=2026-06-21&walk=${WALK_UUID}&near=cairngorms&dmin=2&dmax=6&kmin=5&kmax=20&ascent=800`,
      ),
      VALID_NEAR,
    );
    expect(got).toEqual({
      selectedDay: "2026-06-21",
      selectedWalk: WALK_UUID,
      nearKey: "cairngorms",
      minDuration: "2",
      maxDuration: "6",
      minDistance: "5",
      maxDistance: "20",
      maxAscent: "800",
    });
  });

  it("ignores a near key outside the allow-list", () => {
    expect(readHikeParams(params("near=mordor"), VALID_NEAR).nearKey).toBe("");
  });

  it("ignores a malformed day and negative/NaN numbers", () => {
    const got = readHikeParams(
      params("day=notadate&dmin=-3&kmax=abc"),
      VALID_NEAR,
    );
    expect(got.selectedDay).toBeNull();
    expect(got.minDuration).toBe("");
    expect(got.maxDistance).toBe("");
  });

  it("reads a well-formed walk uuid and ignores a malformed one", () => {
    expect(
      readHikeParams(params(`walk=${WALK_UUID}`), VALID_NEAR).selectedWalk,
    ).toBe(WALK_UUID);
    expect(
      readHikeParams(params("walk=not-a-uuid"), VALID_NEAR).selectedWalk,
    ).toBeNull();
  });
});

describe("writeHikeParams", () => {
  it("omits params at their default value", () => {
    const sp = params("");
    writeHikeParams(sp, {
      selectedDay: null,
      selectedWalk: null,
      nearKey: "",
      minDuration: "",
      maxDuration: "",
      minDistance: "",
      maxDistance: "",
      maxAscent: "",
    });
    expect(sp.toString()).toBe("");
  });

  it("writes only the non-default fields", () => {
    const sp = params("");
    writeHikeParams(sp, {
      selectedDay: "2026-06-21",
      selectedWalk: WALK_UUID,
      nearKey: "skye",
      minDuration: "",
      maxDuration: "4",
      minDistance: "",
      maxDistance: "",
      maxAscent: "",
    });
    expect(sp.get("day")).toBe("2026-06-21");
    expect(sp.get("walk")).toBe(WALK_UUID);
    expect(sp.get("near")).toBe("skye");
    expect(sp.get("dmax")).toBe("4");
    expect(sp.has("dmin")).toBe(false);
    expect(sp.has("ascent")).toBe(false);
  });

  it("round-trips through read", () => {
    const state = {
      selectedDay: "2026-07-01",
      selectedWalk: WALK_UUID,
      nearKey: "fort-william",
      minDuration: "1.5",
      maxDuration: "",
      minDistance: "",
      maxDistance: "12",
      maxAscent: "600",
    };
    const sp = params("");
    writeHikeParams(sp, state);
    expect(readHikeParams(sp, VALID_NEAR)).toEqual(state);
  });

  it("clears the walk param when the card closes", () => {
    const sp = params(`walk=${WALK_UUID}&near=cairngorms`);
    writeHikeParams(sp, {
      selectedDay: null,
      selectedWalk: null,
      nearKey: "cairngorms",
      minDuration: "",
      maxDuration: "",
      minDistance: "",
      maxDistance: "",
      maxAscent: "",
    });
    expect(sp.has("walk")).toBe(false);
    expect(sp.get("near")).toBe("cairngorms");
  });
});
