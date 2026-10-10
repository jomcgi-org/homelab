import { describe, it, expect } from "vitest";
import { describeFreshness } from "./freshness.js";

const NOW = Date.parse("2026-10-04T12:00:00Z");
const base = {
  freshness: "current",
  review_after: "2026-10-05T12:00:00+00:00",
  observed_at: "2026-10-04T08:00:00+00:00",
  last_reviewed_at: null,
  requires_authoritative_observation: false,
  confidence: 0.99,
  verification_state: "verified",
};

describe("describeFreshness", () => {
  it("renders nothing for payloads without freshness (public endpoint)", () => {
    expect(describeFreshness({ body: "x" }, NOW)).toBeNull();
    expect(describeFreshness(null, NOW)).toBeNull();
  });

  it("shows the deadline of a current fact", () => {
    const view = describeFreshness(base, NOW);
    expect(view.state).toBe("current");
    expect(view.label).toBe("CURRENT");
    expect(view.lines[0]).toBe("Review by 2026-10-05 12:00Z (in 24 h)");
    expect(view.lines).toContain("Observed 2026-10-04 08:00Z");
  });

  it("is independent of confidence and verification", () => {
    const due = describeFreshness(
      { ...base, freshness: "due", review_after: "2026-10-04T11:59:00Z" },
      NOW,
    );
    expect(due.state).toBe("due");
    expect(due.lines[0]).toContain("treat as history");
    expect(JSON.stringify(due)).not.toContain("0.99");
    expect(JSON.stringify(due)).not.toContain("verified");
  });

  it("turns a stale 'current' payload due once the deadline passes", () => {
    const view = describeFreshness(base, Date.parse("2026-10-05T12:00:00Z"));
    expect(view.state).toBe("due");
    expect(describeFreshness({ ...base, review_after: null }, NOW).state).toBe(
      "unknown",
    );
  });

  it("crosses the deadline at equality without a new detail payload", () => {
    const deadline = Date.parse(base.review_after);
    expect(describeFreshness(base, deadline - 1).state).toBe("current");
    expect(describeFreshness(base, deadline).state).toBe("due");
    expect(describeFreshness(base, deadline + 1).state).toBe("due");
    expect(base.freshness).toBe("current");
  });

  it("explains why a due fact is still due and flags volatile facts", () => {
    const view = describeFreshness(
      {
        ...base,
        freshness: "due",
        requires_authoritative_observation: true,
        last_reviewed_at: "2026-10-03T12:00:00+00:00",
        last_review_outcome: {
          status: "unsupported",
          reason: "state 'blocked' is not verifiable from GitHub",
        },
      },
      NOW,
    );
    expect(view.volatile).toBe(true);
    expect(view.lines).toContain(
      "Last review unsupported: state 'blocked' is not verifiable from GitHub",
    );
    expect(view.lines).toContain("Last verified 2026-10-03 12:00Z");
    expect(view.lines.at(-1)).toContain("authoritative source");
  });

  it("treats unknown freshness as history without a date", () => {
    const view = describeFreshness(
      { ...base, freshness: "unknown", review_after: null, observed_at: null },
      NOW,
    );
    expect(view.state).toBe("unknown");
    expect(view.lines[0]).toBe(
      "No trustworthy observation date; treat as history",
    );
  });
});
