import { describe, it, expect } from "vitest";
import { highlightMentions } from "./mention-highlight.js";

const strahd = {
  id: 7,
  title: "Strahd von Zarovich",
  kind: "entity",
  entity_type: "npc",
};

describe("highlightMentions", () => {
  it("wraps a plain-text mention with a colored, clickable link to its world page", () => {
    expect(
      highlightMentions("<p>Strahd von Zarovich rules.</p>", [strahd]),
    ).toBe(
      '<p><a class="gmark" data-type="npc" href="/app/grimoire/world?e=7" style="color: var(--grim-type-npc, currentColor)">Strahd von Zarovich</a> rules.</p>',
    );
  });

  it("never touches tag internals: an attribute containing the name is left alone", () => {
    const html = '<p title="Strahd von Zarovich">Strahd von Zarovich</p>';
    const out = highlightMentions(html, [strahd]);
    expect(out).toBe(
      '<p title="Strahd von Zarovich"><a class="gmark" data-type="npc" href="/app/grimoire/world?e=7" style="color: var(--grim-type-npc, currentColor)">Strahd von Zarovich</a></p>',
    );
  });

  it("does not nest an <a> inside an already-open <a> (e.g. a [[wikilink]]): the mention inside is left unwrapped", () => {
    // renderMarkdown emits [[wikilinks]] as <a class="wl dead">...</a> when
    // the title map is empty (both chat call sites pass one). Nesting our
    // <a class="gmark"> inside that anchor would be invalid HTML.
    const html = '<a class="wl dead">Strahd von Zarovich</a> rules.';
    const out = highlightMentions(html, [strahd]);
    expect(out).toBe(html);
  });

  it("matches case-insensitively but preserves the original casing in output", () => {
    const out = highlightMentions("<p>STRAHD VON ZAROVICH awoke.</p>", [
      strahd,
    ]);
    expect(out).toBe(
      '<p><a class="gmark" data-type="npc" href="/app/grimoire/world?e=7" style="color: var(--grim-type-npc, currentColor)">STRAHD VON ZAROVICH</a> awoke.</p>',
    );
  });

  it("matches the HTML-escaped form (name with & matches escaped text)", () => {
    const pair = { id: 9, title: "A & B", kind: "entity", entity_type: "org" };
    const out = highlightMentions("<p>A &amp; B is a guild.</p>", [pair]);
    expect(out).toBe(
      '<p><a class="gmark" data-type="org" href="/app/grimoire/world?e=9" style="color: var(--grim-type-org, currentColor)">A &amp; B</a> is a guild.</p>',
    );
  });

  it("ignores touched items with kind !== entity and items with empty titles", () => {
    const chunk = { id: 1, title: "Strahd von Zarovich", kind: "chunk" };
    const empty = { id: 2, title: "", kind: "entity", entity_type: "npc" };
    const html = "<p>Strahd von Zarovich rules.</p>";
    expect(highlightMentions(html, [chunk, empty])).toBe(html);
  });

  it("does not throw and does not mis-match on regex metacharacters in titles", () => {
    const weird = {
      id: 3,
      title: "K'thriss (the Devourer)",
      kind: "entity",
      entity_type: "npc",
    };
    const html = "<p>K'thriss (the Devourer) stirs.</p>";
    expect(() => highlightMentions(html, [weird])).not.toThrow();
    expect(highlightMentions(html, [weird])).toBe(
      '<p><a class="gmark" data-type="npc" href="/app/grimoire/world?e=3" style="color: var(--grim-type-npc, currentColor)">K\'thriss (the Devourer)</a> stirs.</p>',
    );
  });

  it("overlapping titles: longer titles win (sorted by length desc)", () => {
    const short = {
      id: 4,
      title: "Strahd",
      kind: "entity",
      entity_type: "npc",
    };
    const long = {
      id: 5,
      title: "Strahd von Zarovich",
      kind: "entity",
      entity_type: "npc",
    };
    // Pass short before long in the input array to prove sorting, not
    // input order, decides precedence.
    const out = highlightMentions("<p>Strahd von Zarovich rules.</p>", [
      short,
      long,
    ]);
    expect(out).toBe(
      '<p><a class="gmark" data-type="npc" href="/app/grimoire/world?e=5" style="color: var(--grim-type-npc, currentColor)">Strahd von Zarovich</a> rules.</p>',
    );
  });

  it("allow-lists entity_type before interpolating it into the CSS var name", () => {
    const hostile = {
      id: 6,
      title: "Strahd von Zarovich",
      kind: "entity",
      entity_type: "npc); } body { color:red",
    };
    const out = highlightMentions("<p>Strahd von Zarovich rules.</p>", [
      hostile,
    ]);
    expect(out).toBe(
      '<p><a class="gmark" data-type="class" href="/app/grimoire/world?e=6" style="color: var(--grim-type-class, currentColor)">Strahd von Zarovich</a> rules.</p>',
    );
  });

  it("falls back to class when entity_type is missing", () => {
    const noType = { id: 8, title: "Strahd von Zarovich", kind: "entity" };
    const out = highlightMentions("<p>Strahd von Zarovich rules.</p>", [
      noType,
    ]);
    expect(out).toBe(
      '<p><a class="gmark" data-type="class" href="/app/grimoire/world?e=8" style="color: var(--grim-type-class, currentColor)">Strahd von Zarovich</a> rules.</p>',
    );
  });

  it("URL-encodes the entity id in the href", () => {
    const weirdId = {
      id: "a b&c",
      title: "Strahd von Zarovich",
      kind: "entity",
      entity_type: "npc",
    };
    const out = highlightMentions("<p>Strahd von Zarovich rules.</p>", [
      weirdId,
    ]);
    expect(out).toBe(
      '<p><a class="gmark" data-type="npc" href="/app/grimoire/world?e=a%20b%26c" style="color: var(--grim-type-npc, currentColor)">Strahd von Zarovich</a> rules.</p>',
    );
  });

  it("returns the input unchanged when touched is empty, null, or undefined", () => {
    const html = "<p>Strahd von Zarovich rules.</p>";
    expect(highlightMentions(html, [])).toBe(html);
    expect(highlightMentions(html, null)).toBe(html);
    expect(highlightMentions(html, undefined)).toBe(html);
  });

  // NOTE on idempotency: highlightMentions is NOT safe to call on its own
  // output (the text between two inserted spans could re-match and get
  // double-wrapped). The caller contract, enforced by both callers in
  // chat/+page.svelte, is: call this only on fresh renderMarkdown() output,
  // never on a previously highlighted string. No test exercises the
  // double-call case because the module deliberately does not support it;
  // see the header comment in mention-highlight.js.
});
