import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import type { ReactNode } from "react";
import { HomePage, NotFoundPage } from "./pages";
import { SECTIONS, hrefFor } from "./sections";

function renderPage(page: ReactNode) {
  return render(<Theme theme={neutralTheme}>{page}</Theme>);
}

describe("pages", () => {
  it("shows the home heading and links every section", () => {
    renderPage(<HomePage />);
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Joe McGinley, Scotland.",
      }),
    ).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "Site" });
    for (const section of SECTIONS) {
      expect(
        within(nav)
          .getByRole("link", { name: section.name })
          .getAttribute("href"),
      ).toBe(hrefFor(section));
    }
  });

  it("lists every section on the 404 page", () => {
    renderPage(<NotFoundPage />);
    expect(
      screen.getByRole("heading", { level: 1, name: "This page isn't here." }),
    ).toBeTruthy();
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(4);
  });

  it("renders without inline styles, which the CSP would block", () => {
    // Astryx 0.7.11 skips style injection for built themes (see factory-landing).
    expect(neutralTheme.__built).toBe(true);
    const before = [...document.querySelectorAll("style")];
    const { container } = renderPage(<HomePage />);
    expect([...document.querySelectorAll("style")]).toEqual(before);
    expect(
      [...container.querySelectorAll("[style]")]
        .map((element) => element.getAttribute("style"))
        .filter(Boolean),
    ).toEqual([]);
  });
});
