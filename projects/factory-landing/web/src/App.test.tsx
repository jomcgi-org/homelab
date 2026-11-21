import { act, fireEvent, render, screen } from "@testing-library/react";
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
  needsYou: [
    {
      key: "HOME-20",
      title: "Choose the backup target",
      priority: "high",
      url: "https://factory-plan.jomcgi.dev/issue/20",
      createdAt: "2026-11-14T10:00:00Z",
    },
  ],
  inFlight: [
    {
      key: "HOME-24",
      title: "Factory landing",
      state: "review",
      reviewRounds: 2,
      pr: {
        number: 8000,
        url: "https://github.com/jomcgi-org/homelab/pull/8000",
        ci: "success",
        review: "in_progress",
        queue: "not_queued",
      },
    },
    {
      key: "HOME-25",
      title: "Next change",
      state: "active",
      reviewRounds: 0,
      pr: null,
    },
  ],
  shipped: [
    {
      key: "HOME-19",
      title: "Previous change",
      pr: {
        number: 7999,
        url: "https://github.com/jomcgi-org/homelab/pull/7999",
      },
      mergedAt: "2026-11-14T11:00:00Z",
    },
  ],
  generatedAt: "2026-11-14T12:00:00Z",
};

function renderApp() {
  return render(
    <Theme theme={neutralTheme}>
      <App />
    </Theme>,
  );
}

it("renders tasks, active work and recent merges before the tools", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    json: async () => status,
  } as Response);
  renderApp();
  expect(
    (
      await screen.findByRole("link", {
        name: "HOME-20: Choose the backup target",
      })
    ).getAttribute("href"),
  ).toBe(status.needsYou[0]!.url);
  expect(
    screen
      .getAllByRole("heading", { level: 2 })
      .map((h) => h.textContent)
      .slice(0, 4),
  ).toEqual(["Needs you (1)", "In flight (2)", "Shipped recently", "itsaplan"]);
  expect(
    screen.getByRole("link", { name: "PR #8000" }).getAttribute("href"),
  ).toBe(status.inFlight[0]!.pr!.url);
  expect(
    screen.getByText(
      "Checks: success · Review: in progress · Queue: not queued",
    ),
  ).toBeTruthy();
  expect(screen.getByText("review · Review rounds: 2")).toBeTruthy();
  expect(screen.getByText("No pull request yet.")).toBeTruthy();
  expect(
    screen
      .getByRole("link", { name: "HOME-19: Previous change (#7999)" })
      .getAttribute("href"),
  ).toBe(status.shipped[0]!.pr.url);
});

it("shows the total when the in-flight list is limited", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    json: async () => ({ ...status, inFlightTotal: 110 }),
  } as Response);
  renderApp();
  expect(
    await screen.findByRole("heading", { name: "In flight (2 of 110)" }),
  ).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "HOME-20: Choose the backup target" }),
  ).toBeTruthy();
});

it("renders each empty state", async () => {
  renderApp();
  await screen.findByText("Nothing needs you right now.");
  expect(screen.getByRole("heading", { name: "Needs you (0)" })).toBeTruthy();
  expect(screen.getByText("No work in flight.")).toBeTruthy();
  expect(screen.getByText("No recent merges.")).toBeTruthy();
});

it("shows Needs you as unavailable and keeps the other sections", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    json: async () => ({ ...status, needsYou: null }),
  } as Response);
  renderApp();
  await screen.findByText(
    "Unavailable: no itsaplan member @joe in the factory's project.",
  );
  expect(screen.getByRole("heading", { name: "Needs you" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "In flight (2)" })).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "HOME-19: Previous change (#7999)" }),
  ).toBeTruthy();
  expect(screen.queryByText("Nothing needs you right now.")).toBeNull();
});

it.each(["http", "network", "malformed"])(
  "keeps the tools usable after a %s failure",
  async (failure) => {
    if (failure === "network")
      vi.mocked(fetch).mockRejectedValue(new Error("offline"));
    else
      vi.mocked(fetch).mockResolvedValue({
        ok: failure !== "http",
        json: async () => ({ needsYou: [null] }),
      } as Response);
    renderApp();
    await screen.findByText(
      "Factory status is unavailable. You can still open the tools below.",
    );
    expect(screen.getByRole("link", { name: "Open itsaplan" })).toBeTruthy();
    expect(screen.queryByText("Nothing needs you right now.")).toBeNull();
  },
);

it("refreshes every 30 seconds, recovers from failure and stops on unmount", async () => {
  vi.useFakeTimers();
  vi.mocked(fetch).mockRejectedValueOnce(new Error("offline"));
  const view = renderApp();
  await act(async () => {
    await Promise.resolve();
  });
  const notice =
    "Factory status is unavailable. You can still open the tools below.";
  expect(screen.getByText(notice).getAttribute("role")).toBe("status");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(29_999);
  });
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(screen.queryByText(notice)).toBeNull();
  expect(screen.getByText("No work in flight.")).toBeTruthy();
  view.unmount();
  await vi.advanceTimersByTimeAsync(30_000);
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("confirms a displayed choice once and returns from an emptied last page", async () => {
  const revision = "a".repeat(64);
  const needsYou = Array.from({ length: 6 }, (_, i) => ({
    ...status.needsYou[0],
    key: `HOME-${i + 1}`,
    choices: ["Keep", "Change"],
    choiceRevision: revision,
  }));
  vi.mocked(fetch)
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({ ...status, needsYou }),
    } as Response)
    .mockResolvedValueOnce(new Response(null, { status: 204 }));
  const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
  vi.stubGlobal("confirm", confirm);
  try {
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    expect(
      screen
        .getByRole("link", { name: "Open HOME-6 in itsaplan" })
        .getAttribute("href"),
    ).toBe(status.needsYou[0]!.url);
    fireEvent.click(screen.getByRole("button", { name: /HOME-6/ }));
    const recommended = screen.getByRole("button", {
      name: "Keep (Recommended)",
    });
    fireEvent.click(recommended);
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(recommended);
    await screen.findByRole("heading", { name: "Needs you (5)" });
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(fetch).toHaveBeenLastCalledWith("/api/decide", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "If-Match": `"${revision}"`,
      },
      body: JSON.stringify({ key: "HOME-6", choice: 1 }),
    });
    expect(screen.queryByRole("button", { name: /HOME-6/ })).toBeNull();
    expect(screen.getByRole("button", { name: /HOME-1/ })).toBeTruthy();
  } finally {
    confirm.mockRestore();
  }
});

it("offers the same choices again after the issue leaves Needs you", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
  const item = {
    ...status.needsYou[0],
    choices: ["Proceed"],
    choiceRevision: "b".repeat(64),
  };
  const snapshot = (needsYou: (typeof item)[]) =>
    Response.json({ ...status, needsYou });
  vi.mocked(fetch)
    .mockResolvedValueOnce(snapshot([item]))
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
    .mockResolvedValueOnce(snapshot([item]))
    .mockResolvedValueOnce(snapshot([]))
    .mockResolvedValueOnce(snapshot([item]));
  await act(async () => {
    renderApp();
  });
  fireEvent.click(screen.getByRole("button", { name: /HOME-20/ }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /Recommended/ }));
  });
  for (const expected of [false, false, true]) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(Boolean(screen.queryByRole("button", { name: /HOME-20/ }))).toBe(
      expected,
    );
  }
});
