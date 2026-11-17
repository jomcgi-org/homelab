import { render } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { HomePage, NotFoundPage } from "./pages";

// Structural accessibility rules on the rendered pages. happy-dom has no
// layout, so colour contrast is left to contrast.test.tsx and the browser
// check in the Kargo dev stage.
describe.each([
  ["home", <HomePage key="home" />],
  ["404", <NotFoundPage key="404" />],
])("%s page", (_name, page) => {
  it("has no axe violations", async () => {
    const { container } = render(<Theme theme={neutralTheme}>{page}</Theme>);
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
