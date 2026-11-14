import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { App } from "./App";
import { paths } from "./paths";

it("links to the operator tools and keeps unrouted kagent unavailable", () => {
  render(
    <Theme theme={neutralTheme}>
      <App />
    </Theme>,
  );
  for (const [label, href] of [
    ["Open itsaplan", paths.plan],
    ["Open Grafana", paths.grafana],
    ["Open ArgoCD", paths.argocd],
    ["Open GitHub", paths.github],
    ["Open factory PRs", paths.pullRequests],
  ]) {
    expect(screen.getByRole("link", { name: label }).getAttribute("href")).toBe(
      href,
    );
  }
  expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(5);
  expect(screen.getByText("Coming soon")).toBeTruthy();
  expect(screen.queryByRole("link", { name: /kagent/i })).toBeNull();
});
