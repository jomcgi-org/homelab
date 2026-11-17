import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { render } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { PAGES } from "./pages";

/** Every HTML entry in the app, at any depth, outside build and package dirs. */
function htmlEntries(dir = ".", prefix = ""): string[] {
  return readdirSync(join(dir, prefix), { withFileTypes: true }).flatMap(
    (entry) => {
      const path = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        return ["dist", "node_modules", "public"].includes(entry.name)
          ? []
          : htmlEntries(dir, path);
      }
      return entry.name.endsWith(".html") ? [path] : [];
    },
  );
}

describe("page registry", () => {
  it("registers every HTML entry, and nothing else", () => {
    expect(htmlEntries().sort()).toEqual(Object.keys(PAGES).sort());
  });

  it.each(Object.keys(PAGES))("%s mounts its registered page", (entry) => {
    const html = readFileSync(entry, "utf8");
    const script = html.match(/<script[^>]*\ssrc="\.\/([^"]+)"/)?.[1];
    expect(script, `${entry} has no entry script`).toBeDefined();
    expect(readFileSync(script!, "utf8")).toContain(`PAGES["${entry}"]`);
  });
});

// Structural accessibility rules on every registered page. happy-dom has no
// layout, so colour contrast is left to contrast.test.tsx and the browser
// check in the Kargo dev stage.
describe.each(Object.entries(PAGES))("%s", (_entry, Page) => {
  it("has no axe violations", async () => {
    const { container } = render(
      <Theme theme={neutralTheme}>
        <Page />
      </Theme>,
    );
    const results = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(
      results.violations.map((violation) => ({
        id: violation.id,
        nodes: violation.nodes.map((node) => node.target.join(" ")),
      })),
    ).toEqual([]);
  });
});
