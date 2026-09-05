import { describe, expect, it } from "vitest";
import { PREEMPTION_COPY, preemptionCopy } from "./preemption-copy.js";

describe("preemptionCopy", () => {
  it.each([
    ["confirming", "It is checking whether the machine is really gone."],
    ["restoring", "It is coming back with its data."],
    ["cold", "It is starting fresh."],
  ])("appends the %s phase", (phase, phaseCopy) => {
    expect(preemptionCopy(phase)).toBe(`${PREEMPTION_COPY} ${phaseCopy}`);
  });
});
