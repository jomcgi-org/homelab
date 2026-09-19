import { describe, it, expect } from "vitest";
import {
  edgeRows,
  eventLines,
  parseOther,
  relativeTime,
  directionLabel,
} from "./work-item-view.js";

describe("work-item-view helpers", () => {
  describe("edgeRows", () => {
    it("groups edges by kind and direction", () => {
      const doc = {
        item: { id: 1 },
        edges_in: [
          { id: 1, from_id: 2, kind: "blocks", source: "manual" },
          { id: 2, from_id: 3, kind: "parent", source: "github_body" },
        ],
        edges_out: [
          { id: 3, to_id: 4, kind: "blocks", source: "manual" },
          { id: 4, to_id: 5, kind: "parent", source: "decision" },
          { id: 5, to_id: 6, kind: "supersedes", source: "manual" },
        ],
        events: [],
      };

      const rows = edgeRows(doc);

      expect(rows.blocked_by).toHaveLength(1);
      expect(rows.blocks).toHaveLength(1);
      expect(rows.parent).toHaveLength(1);
      expect(rows.children).toHaveLength(1);
      expect(rows.supersedes).toHaveLength(1);
    });

    it("returns empty edges for null document", () => {
      const rows = edgeRows(null);
      expect(rows).toEqual({});
    });
  });

  describe("parseOther", () => {
    it("parses issue number format", () => {
      const result = parseOther("#123");
      expect(result).toEqual({ type: "issue", number: 123 });
    });

    it("parses work item id format", () => {
      const result = parseOther("456");
      expect(result).toEqual({ type: "id", id: 456 });
    });

    it("returns null for invalid format", () => {
      expect(parseOther("#abc")).toBeNull();
      expect(parseOther("")).toBeNull();
      expect(parseOther("  ")).toBeNull();
    });

    it("trims whitespace", () => {
      const result = parseOther("  789  ");
      expect(result).toEqual({ type: "id", id: 789 });
    });
  });

  describe("relativeTime", () => {
    it("formats seconds ago", () => {
      const now = 1000000;
      const timestamp = 1000000 - 45000; // 45 seconds ago
      expect(relativeTime(timestamp, now)).toBe("45s ago");
    });

    it("formats minutes ago", () => {
      const now = 1000000;
      const timestamp = 1000000 - 5 * 60000; // 5 minutes ago
      expect(relativeTime(timestamp, now)).toBe("5m ago");
    });

    it("formats hours ago", () => {
      const now = 1000000;
      const timestamp = 1000000 - 3 * 3600000; // 3 hours ago
      expect(relativeTime(timestamp, now)).toBe("3h ago");
    });

    it("formats days ago", () => {
      const now = 1000000;
      const timestamp = 1000000 - 7 * 86400000; // 7 days ago
      expect(relativeTime(timestamp, now)).toBe("7d ago");
    });
  });

  describe("directionLabel", () => {
    it("returns correct labels for blocks", () => {
      expect(directionLabel("blocks", "out")).toBe("this blocks");
      expect(directionLabel("blocks", "in")).toBe("this is blocked by");
    });

    it("returns correct labels for parent", () => {
      expect(directionLabel("parent", "out")).toBe("parent of");
      expect(directionLabel("parent", "in")).toBe("child of");
    });

    it("returns correct labels for supersedes", () => {
      expect(directionLabel("supersedes", "out")).toBe("supersedes");
      expect(directionLabel("supersedes", "in")).toBe("superseded by");
    });
  });

  describe("eventLines", () => {
    it("maps events with relative time", () => {
      const now = 1000000;
      const doc = {
        item: { id: 1 },
        edges_in: [],
        edges_out: [],
        events: [
          {
            id: 1,
            version: 1,
            op: "mint",
            author: "system",
            created_at: new Date(1000000 - 60000).toISOString(),
          },
        ],
      };

      const lines = eventLines(doc, now);

      expect(lines).toHaveLength(1);
      expect(lines[0].relativeTime).toMatch(/ago/);
    });

    it("returns empty for no events", () => {
      const doc = {
        item: { id: 1 },
        edges_in: [],
        edges_out: [],
        events: [],
      };

      const lines = eventLines(doc, 1000000);
      expect(lines).toEqual([]);
    });
  });
});
