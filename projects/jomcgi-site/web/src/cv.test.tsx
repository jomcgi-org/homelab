import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CvPage } from "./cv-page";
import { contact, jobs, skills } from "./cv-data";
import { Inline } from "./Inline";
import { Root } from "./root";

describe("CV page", () => {
  it("shows the name, the four parts and every job", () => {
    render(
      <Root>
        <CvPage />
      </Root>,
    );
    expect(
      screen.getByRole("heading", { level: 1, name: "Joe McGinley" }),
    ).toBeTruthy();
    expect(
      screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent),
    ).toEqual([
      "Profile",
      "Work Experience",
      "Personal Engineering",
      "Technical Expertise",
    ]);
    const companies = screen
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(companies).toEqual([
      ...jobs.map((job) => job.company),
      "Earlier Career",
    ]);
    for (const category of skills) {
      expect(screen.getByText(category.label)).toBeTruthy();
    }
  });

  it("links contact details and the parts of the page", () => {
    render(
      <Root>
        <CvPage />
      </Root>,
    );
    expect(
      screen.getByRole("link", { name: contact.email }).getAttribute("href"),
    ).toBe(`mailto:${contact.email}`);
    const index = screen.getByRole("navigation", { name: "On this page" });
    for (const link of within(index).getAllByRole("link")) {
      const id = link.getAttribute("href")!.slice(1);
      expect(document.getElementById(id), id).toBeTruthy();
    }
  });
});

describe("Inline", () => {
  it("renders bold and links, and leaves other text as text", () => {
    const { container } = render(
      <Inline text="a **b** [c](https://example.com/d) *e* f" />,
    );
    expect(container.querySelector("strong")!.textContent).toBe("b");
    const link = container.querySelector("a")!;
    expect(link.textContent).toBe("c");
    expect(link.getAttribute("href")).toBe("https://example.com/d");
    expect(link.getAttribute("rel")).toBe("noreferrer");
    expect(container.textContent).toBe("a b c *e* f");
  });
});
