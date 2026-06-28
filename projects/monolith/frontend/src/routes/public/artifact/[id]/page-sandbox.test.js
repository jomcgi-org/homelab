/**
 * Security invariant tests for the artifact wrapper page (ADR 024).
 *
 * We read the +page.svelte source directly and assert on the text because:
 *  - The sandboxed iframe is the primary security boundary.
 *  - allow-same-origin would re-grant the jomcgi.dev origin to artifact code,
 *    defeating the sandbox entirely.
 *  - A regex over the source is deterministic and does not require a JSDOM
 *    render of SvelteKit-specific syntax ($app/stores, reactive $: labels).
 *
 * These tests MUST NOT be deleted or weakened without an explicit ADR revision.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, dirname } from "node:path";

const pageSource = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "+page.svelte"),
  "utf8",
);

describe("+page.svelte iframe sandbox invariant (ADR 024)", () => {
  it('sandbox attribute is exactly "allow-scripts"', () => {
    // Must match sandbox="allow-scripts" with no additional tokens.
    // The attribute value must be the complete string "allow-scripts".
    expect(pageSource).toMatch(/sandbox="allow-scripts"/);
  });

  it('sandbox attribute value does not include "allow-same-origin"', () => {
    // This would re-grant the jomcgi.dev origin to the sandboxed artifact.
    // We extract the attribute VALUE to avoid false-positives from warning
    // comments in the source that mention the forbidden token by name.
    const match = pageSource.match(/sandbox="([^"]*)"/);
    expect(match).toBeTruthy();
    expect(match[1]).not.toContain("allow-same-origin");
  });

  it('sandbox attribute value does not include "allow-forms"', () => {
    const match = pageSource.match(/sandbox="([^"]*)"/);
    expect(match).toBeTruthy();
    expect(match[1]).not.toContain("allow-forms");
  });

  it('sandbox attribute value does not include "allow-popups"', () => {
    const match = pageSource.match(/sandbox="([^"]*)"/);
    expect(match).toBeTruthy();
    expect(match[1]).not.toContain("allow-popups");
  });

  it('sandbox attribute value does not include "allow-top-navigation"', () => {
    const match = pageSource.match(/sandbox="([^"]*)"/);
    expect(match).toBeTruthy();
    expect(match[1]).not.toContain("allow-top-navigation");
  });

  it("renders an <iframe> element", () => {
    expect(pageSource).toMatch(/<iframe\b/);
  });
});
