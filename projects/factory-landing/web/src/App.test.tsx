import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { App } from "./App";
import { paths } from "./paths";

it("links to all operator tools, including live kagent sessions", () => {
  render(
    <Theme theme={neutralTheme}>
      <App />
    </Theme>,
  );
  for (const [label, href] of [
    ["Open itsaplan", paths.plan],
    ["Open kagent", "https://factory-agents.jomcgi.dev"],
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
  expect(screen.queryByText("Coming soon")).toBeNull();
});
