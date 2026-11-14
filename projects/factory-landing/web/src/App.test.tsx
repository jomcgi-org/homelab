import { act, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { App } from "./App";
import { paths } from "./paths";

it("links to all operator tools, including live kagent sessions", async () => {
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
  await screen.findByText("Nothing needs you right now.");
  expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(8);
  expect(screen.queryByText("Coming soon")).toBeNull();
});

const status = {
  needsYou: [{ key: "HOME-20", title: "Choose the backup target", priority: "high", url: "https://factory-plan.jomcgi.dev/issue/20", createdAt: "2026-11-14T10:00:00Z" }],
  inFlight: [
    { key: "HOME-24", title: "Factory landing", state: "review", reviewRounds: 2, pr: { number: 8000, url: "https://github.com/jomcgi-org/homelab/pull/8000", ci: "success", review: "in_progress", queue: "not_queued" } },
    { key: "HOME-25", title: "Next change", state: "active", reviewRounds: 0, pr: null },
  ],
  shipped: [{ key: "HOME-19", title: "Previous change", pr: { number: 7999, url: "https://github.com/jomcgi-org/homelab/pull/7999" }, mergedAt: "2026-11-14T11:00:00Z" }],
  generatedAt: "2026-11-14T12:00:00Z",
};

function renderApp() {
  return render(<Theme theme={neutralTheme}><App /></Theme>);
}

it("renders tasks, active work and recent merges before the tools", async () => {
  vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => status } as Response);
  renderApp();
  expect((await screen.findByRole("link", { name: "HOME-20: Choose the backup target" })).getAttribute("href")).toBe(status.needsYou[0]!.url);
  expect(screen.getAllByRole("heading", { level: 2 }).map(h => h.textContent).slice(0, 4)).toEqual(["Needs you (1)", "In flight (2)", "Shipped recently", "itsaplan"]);
  expect(screen.getByRole("link", { name: "PR #8000" }).getAttribute("href")).toBe(status.inFlight[0]!.pr!.url);
  expect(screen.getByText("Checks: success · Review: in progress · Queue: not queued")).toBeTruthy();
  expect(screen.getByText("review · Review rounds: 2")).toBeTruthy();
  expect(screen.getByText("No pull request yet.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "HOME-19: Previous change (#7999)" }).getAttribute("href")).toBe(status.shipped[0]!.pr.url);
});

it("renders each empty state", async () => {
  renderApp();
  await screen.findByText("Nothing needs you right now.");
  expect(screen.getByRole("heading", { name: "Needs you (0)" })).toBeTruthy();
  expect(screen.getByText("No work in flight.")).toBeTruthy();
  expect(screen.getByText("No recent merges.")).toBeTruthy();
});

it.each(["http", "network", "malformed"])("keeps the tools usable after a %s failure", async failure => {
  if (failure === "network") vi.mocked(fetch).mockRejectedValue(new Error("offline"));
  else vi.mocked(fetch).mockResolvedValue({ ok: failure !== "http", json: async () => ({ needsYou: [null] }) } as Response);
  renderApp();
  await screen.findByText("Factory status is unavailable. You can still open the tools below.");
  expect(screen.getByRole("link", { name: "Open itsaplan" })).toBeTruthy();
  expect(screen.queryByText("Nothing needs you right now.")).toBeNull();
});

it("refreshes every 30 seconds, recovers from failure and stops on unmount", async () => {
  vi.useFakeTimers();
  vi.mocked(fetch).mockRejectedValueOnce(new Error("offline"));
  const view = renderApp();
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByRole("status").textContent).toContain("unavailable");
  await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.getByText("No work in flight.")).toBeTruthy();
  view.unmount();
  await vi.advanceTimersByTimeAsync(30_000);
  expect(fetch).toHaveBeenCalledTimes(2);
});
