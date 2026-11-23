import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { Shell } from "./Shell";

const menu = () => document.querySelector<HTMLDetailsElement>(".site-menu")!;

it("folds the long group into a menu that holds every link", () => {
  render(<Shell>page</Shell>);
  expect(menu().querySelector("summary")?.textContent).toBe("Apps");
  expect(menu().querySelectorAll("a").length).toBeGreaterThan(3);
  // The other groups stay as plain links in the row.
  expect(screen.getByRole("link", { name: "Blog" })).toBeTruthy();
});

it("closes the open menu on Escape and on a press outside it", () => {
  render(<Shell>page</Shell>);
  menu().open = true;
  fireEvent.pointerDown(menu().querySelector("a")!);
  expect(menu().open).toBe(true);
  fireEvent.keyDown(document, { key: "Escape" });
  expect(menu().open).toBe(false);
  expect(document.activeElement).toBe(menu().querySelector("summary"));
  menu().open = true;
  fireEvent.pointerDown(document.body);
  expect(menu().open).toBe(false);
});
