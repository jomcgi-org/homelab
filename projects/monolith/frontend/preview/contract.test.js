import { describe, expect, it } from "vitest";
import { load } from "../src/routes/public/blog/[slug]/+page.server.js";
import fixture from "./fixtures/blog-page.json";
import recording from "./fixtures/qwen-replay.json";
import { assertBlogPage } from "./blog-contract.js";
import { incidentGraph } from "../src/lib/public/posts/incident-graph.js";

describe("same-commit blog loader contract", () => {
  it("matches the shared UI fixture exactly after running the real server loader", () => {
    const headers = {};
    const actual = load({
      params: { slug: "125b-on-a-4090" },
      setHeaders: (value) => Object.assign(headers, value),
    });
    assertBlogPage(actual);
    assertBlogPage(fixture);
    expect(actual).toStrictEqual(fixture);
    expect(headers.etag).toContain("blog-125b-on-a-4090");
    expect(actual.toc[0].children).toHaveLength(1);
    expect(actual.sections).toHaveLength(2);
  });
  it("retains the real loader's missing-post failure", () => {
    expect(() =>
      load({ params: { slug: "missing" }, setHeaders() {} }),
    ).toThrow();
  });
  it("makes contract drift detectable", () => {
    const changed = { ...fixture, sections: "invalid" };
    expect(() => assertBlogPage(changed)).toThrow(TypeError);
    expect(changed).not.toStrictEqual(
      load({ params: { slug: fixture.slug }, setHeaders() {} }),
    );
  });
});

describe("synthetic inference recording", () => {
  it("has a deterministic bounded timeline accepted by the production graph parser", () => {
    const turn = recording.turns[0];
    expect(recording.build).toBe("synthetic-fixture");
    expect(recording.source.url).toBe("#synthetic-source");
    expect(turn.events.map((e) => e.at)).toStrictEqual([
      1000, 1500, 2000, 2500,
    ]);
    expect(turn.events.every((e) => e.at <= turn.durationMs)).toBe(true);
    const graph = incidentGraph(
      turn.events.map((e) => e.content).join(""),
      true,
    );
    expect(graph.nodes).toHaveLength(2);
    expect(graph.edges).toHaveLength(1);
    expect(graph.summary.label).toBe("Synthetic fixture boundary");
  });
});

describe("typed fixture shape", () => {
  it.each([
    { ...fixture, title: 42 },
    { ...fixture, unexpected: true },
    { ...fixture, toc: [{ ...fixture.toc[0], children: ["invalid"] }] },
  ])("rejects malformed server data", (value) => {
    expect(() => assertBlogPage(value)).toThrow(TypeError);
  });
});
