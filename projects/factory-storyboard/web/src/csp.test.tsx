import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { contentSecurityPolicy } from "../csp";
import { App } from "./App";
import { mockFiles } from "./test-fixtures";

it.each(["/", "/goals/reading-list", "/issues/DEMO-104", "/sessions/demo-build"])("renders %s without inline styles, including expanded tools and dark theme", async (path) => {
  expect(neutralTheme.__built).toBe(true);
  expect(contentSecurityPolicy).toContain("style-src 'self'");
  expect(contentSecurityPolicy).not.toMatch(/unsafe-inline|unsafe-eval/);
  const styles = [...document.querySelectorAll("style")];
  window.history.replaceState(null, "", path);
  localStorage.setItem("factory-storyboard-theme", "light");
  mockFiles();
  const { container } = render(<App />);
  await screen.findByText(/Every goal, issue, session and measurement here is made up/);
  for (const summary of container.querySelectorAll("summary")) fireEvent.click(summary);
  fireEvent.click(screen.getByRole("button", { name: "Use dark theme" }));
  expect([...document.querySelectorAll("style")]).toEqual(styles);
  expect(container.querySelectorAll("[style]")).toHaveLength(0);
  expect(container.querySelectorAll("script")).toHaveLength(0);
});
