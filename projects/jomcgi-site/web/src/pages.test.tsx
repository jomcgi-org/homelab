import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import type { ReactNode } from "react";
import { HomePage } from "./home-page";
import { NotFoundPage } from "./not-found-page";
import { HOME_KEYS, sectionHref } from "./home-data";
import { GROUPS, SECTIONS, sectionsIn } from "./sections";

function renderPage(page: ReactNode) {
  return render(<Theme theme={neutralTheme}>{page}</Theme>);
}

describe("pages", () => {
  it("shows the home heading and original keypad links", () => {
    renderPage(<HomePage />);
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Joe McGinley, Scotland.",
      }),
    ).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "Site" });
    for (const key of HOME_KEYS) {
      expect(
        within(nav)
          .getByRole("link", {
            name: key.legend,
          })
          .getAttribute("href"),
      ).toBe(sectionHref(key.section));
    }
  });

  it("links every live section from the 404 page, and nothing else", () => {
    // HOME-261: jomcgi.dev/* answers every unknown path with this page.
    const { container } = renderPage(<NotFoundPage />);
    expect(
      screen.getByRole("heading", { level: 1, name: "This page isn't here." }),
    ).toBeTruthy();
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(4);
    const live = GROUPS.flatMap(sectionsIn).map(({ path }) => path);
    const cards = [...container.querySelectorAll("a.site-card-link")].map(
      (link) => link.getAttribute("href"),
    );
    expect(cards).toEqual(live);
    for (const section of SECTIONS.filter(({ live }) => !live)) {
      expect(screen.queryAllByRole("link", { name: section.name })).toEqual([]);
    }
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
