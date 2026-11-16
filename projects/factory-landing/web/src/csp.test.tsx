import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { App } from "./App";
import { headers } from "../../server.mjs";

it("allows only same-origin status reads and renders without blocked styles", async () => {
  expect(headers["Content-Security-Policy"]).toContain("connect-src 'self';");
  expect(headers["Content-Security-Policy"]).toContain("style-src 'self';");
  expect(headers["Content-Security-Policy"]).not.toMatch(
    /unsafe-inline|unsafe-eval/,
  );
  // Astryx 0.7.11 Theme.tsx skips useThemeStyleInjection for __built themes.
  // main.tsx imports theme-neutral/theme.css into the external Vite bundle.
  expect(neutralTheme.__built).toBe(true);
  const existingStyles = [...document.querySelectorAll("style")];
  const { container } = render(
    <Theme theme={neutralTheme}>
      <App />
    </Theme>,
  );
  await screen.findByText("Nothing needs you right now.");
  expect([...document.querySelectorAll("style")]).toEqual(existingStyles);
  expect(
    [...container.querySelectorAll("[style]")]
      .map((element) => element.getAttribute("style"))
      .filter(Boolean),
  ).toEqual([]);
});
