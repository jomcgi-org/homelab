import { render } from "@testing-library/react";
import { expect, it } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { App } from "./App";

it("renders with the built theme without styles blocked by the CSP", () => {
  // Astryx 0.7.11 Theme.tsx skips useThemeStyleInjection for __built themes.
  // main.tsx imports theme-neutral/theme.css into the external Vite bundle.
  expect(neutralTheme.__built).toBe(true);
  const existingStyles = [...document.querySelectorAll("style")];
  const { container } = render(
    <Theme theme={neutralTheme}>
      <App />
    </Theme>,
  );
  expect([...document.querySelectorAll("style")]).toEqual(existingStyles);
  expect(
    [...container.querySelectorAll("[style]")]
      .map((element) => element.getAttribute("style"))
      .filter(Boolean),
  ).toEqual([]);
});
