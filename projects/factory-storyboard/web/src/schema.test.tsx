import { describe, expect, it } from "vitest";
import type { Documents, Goal, Issue, Session, Story } from "../../schema/types";
import { states } from "../../schema/types";
import { validate } from "../../schema/validate";
import { sha256 } from "./data";
import { fixtureFiles, fixtureManifest } from "./test-fixtures";

const files = fixtureFiles();
const manifest = fixtureManifest();
const read = (path: string) => JSON.parse(files.get(`/data/${path}`)!);
const story = read(manifest.story) as Story;
const session = read(manifest.sessions["demo-build"]!) as Session;
describe("snapshot v1 fixtures", () => {
  it("validates every object against the JSON Schema and verifies its byte address", async () => {
    expect(validate("manifest", manifest)).toBe(true);
    const paths = [manifest.story, ...Object.values(manifest.goals), ...Object.values(manifest.issues), ...Object.values(manifest.sessions)];
    expect(new Set(paths).size).toBe(files.size - 1);
    for (const path of paths) {
      const raw = files.get(`/data/${path}`)!;
      const data = JSON.parse(raw) as Documents["story" | "goal" | "issue" | "session"];
      expect(validate(data.kind, data), path).toBe(true);
      expect(path).toBe(`objects/${await sha256(new TextEncoder().encode(raw).buffer)}.json`);
    }
  });
  it("covers each state, empty goals, withheld turns, tools, diffs and an unknown stat", () => {
    const issues = Object.values(manifest.issues).map((path) => read(path) as Issue);
    expect(new Set(issues.map((issue) => issue.state))).toEqual(new Set(states));
    expect((read(manifest.goals["quiet-shelf"]!) as Goal).issueKeys).toEqual([]);
    expect(session.turns.length).toBeGreaterThan(20);
    expect(session.turns.some((turn) => turn.withheld)).toBe(true);
    expect(session.turns.some((turn) => !turn.withheld && turn.toolCalls.length > 1)).toBe(true);
    expect(session.turns.some((turn) => !turn.withheld && turn.diff)).toBe(true);
    expect(story.narrative.join(" ")).toContain("{{stat:unknown}}");
  });
  it("resolves every typed cross-link within the fixture manifest", () => {
    for (const summary of story.goals) expect(manifest.goals[summary.goalId]).toBeDefined();
    for (const summary of [...story.workInProgress, ...story.recentlyShipped]) {
      expect(manifest.issues[summary.issueKey]).toBeDefined();
      expect(manifest.goals[summary.goalId]).toBeDefined();
    }
    for (const path of Object.values(manifest.goals)) {
      for (const key of (read(path) as Goal).issueKeys) expect(manifest.issues[key]).toBeDefined();
    }
    for (const path of Object.values(manifest.issues)) {
      const issue = read(path) as Issue;
      expect(manifest.goals[issue.goalId]).toBeDefined();
      for (const event of issue.timeline) if (event.kind === "session") {
        const transcript = read(manifest.sessions[event.sessionId]!) as Session;
        expect(transcript.issueKey).toBe(issue.key);
        expect(transcript.turns.some((turn) => turn.number === event.turn)).toBe(true);
      }
    }
    for (const path of Object.values(manifest.sessions)) {
      const transcript = read(path) as Session;
      expect(manifest.issues[transcript.issueKey]).toBeDefined();
      expect(manifest.goals[transcript.goalId]).toBeDefined();
    }
  });
});

describe("schema boundaries", () => {
  it.each([
    ["unsupported version", { ...story, version: 2 }],
    ["unknown fields", { ...story, url: "https://example.invalid" }],
    ["literal narrative numbers", { ...story, narrative: ["I shipped 42 issues."] }],
    ["incomplete placeholders", { ...story, narrative: ["I shipped {{stat:broken} issues."] }],
    ["numeric strings", { ...story, stats: { shipped: { label: "Shipped", value: "42", unit: "issues" } } }],
    ["invalid states", { ...story, goals: [{ ...story.goals[0], state: "running" }] }],
    ["invalid rates", { ...story, charts: { ...story.charts, firstPassReviewRate: [{ label: "Week", value: 101 }] } }],
  ])("rejects %s", (_, value) => { expect(validate("story", value)).toBe(false); });
  it("rejects withheld content, missing fields, unsafe references and unsafe numbers", () => {
    const hidden = session.turns.find((turn) => turn.withheld)!;
    expect(validate("session", { ...session, turns: [{ ...hidden, text: "must not leak" }] })).toBe(false);
    expect(validate("session", { ...session, turns: [{ ...hidden, number: Number.MAX_SAFE_INTEGER + 1 }] })).toBe(false);
    expect(validate("session", { ...session, turns: [{ withheld: false }] })).toBe(false);
    expect(validate("manifest", { ...manifest, story: "../private.json" })).toBe(false);
    expect(validate("manifest", { ...manifest, goals: { "javascript:alert(1)": manifest.story } })).toBe(false);
    expect(validate("story", { ...story, stats: { invalid: { label: "Invalid", value: Infinity, unit: "issues" } } })).toBe(false);
    expect(validate("goal", null)).toBe(false);
  });
});
