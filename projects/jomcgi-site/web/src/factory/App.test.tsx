import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { FactoryPage as App } from "../factory-page";
import { render as prerender } from "../prerender";
import { Root } from "../root";
import { Pages, withheldMessage } from "./Pages";
import {
  fixtureFiles,
  fixtureManifest,
  fixtureSnapshot,
  mockFiles,
  replaceObject,
} from "./test-fixtures";

beforeEach(() => window.history.replaceState(null, "", "/factory"));

it.each([
  ["/factory", "A reading list that keeps your place"],
  ["/factory/goals/reading-list", "Return to the reading list"],
  ["/factory/issues/DEMO-104", "Open a saved reading list"],
  ["/factory/sessions/demo-build", "Remember the saved reading list"],
])(
  "hydrates the site's pre-rendered Factory shell from fixtures at %s",
  async (path, title) => {
    window.history.replaceState(null, "", path);
    mockFiles();
    const container = document.createElement("div");
    container.innerHTML = prerender("factory/index.html");
    document.body.appendChild(container);
    const onRecoverableError = vi.fn();
    render(
      <Root>
        <App />
      </Root>,
      { container, hydrate: true, onRecoverableError },
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: title }),
    ).toBeDefined();
    expect(
      screen.getByText(
        /Every goal, issue, session and measurement here is made up/,
      ),
    ).toBeDefined();
    // The snapshot resolves outside act(), so the title effect can run just
    // after the heading commits.
    await waitFor(() =>
      expect(document.title).toBe(`${title} · Factory · jomcgi.dev`),
    );
    expect(onRecoverableError).not.toHaveBeenCalled();
  },
);

it("shows all the story charts with tables and fills the unknown stat", async () => {
  const snapshot = await fixtureSnapshot();
  render(<Pages snapshot={snapshot} path="/factory" />);
  expect(
    screen.getByText(/A future measurement reads – until it is available/),
  ).toBeDefined();
  expect(screen.getAllByRole("img")).toHaveLength(4);
  for (const summary of screen.getAllByText(/View values/))
    fireEvent.click(summary);
  expect(document.querySelectorAll("table caption")).toHaveLength(4);
  expect(
    screen.getByRole("heading", { name: "Work in progress" }),
  ).toBeDefined();
  expect(
    screen.getByRole("heading", { name: "Recently shipped" }),
  ).toBeDefined();
});

it("shows a goal's issues by state and an empty goal without a misleading chart", async () => {
  const snapshot = await fixtureSnapshot();
  const { rerender } = render(
    <Pages snapshot={snapshot} path="/factory/goals/reading-list" />,
  );
  for (const state of [
    "Working",
    "In review",
    "In merge queue",
    "Shipped",
    "Stopped",
  ])
    expect(screen.getByRole("heading", { name: state })).toBeDefined();
  expect(screen.getByText("Goal burn-up")).toBeDefined();
  rerender(<Pages snapshot={snapshot} path="/factory/goals/quiet-shelf" />);
  expect(screen.getByText("No issues yet.")).toBeDefined();
  expect(screen.getByText("No measurements yet.")).toBeDefined();
  expect(screen.queryByRole("img")).toBeNull();
});

it("keeps issue history in one timeline, with typed PR and session links", async () => {
  const snapshot = await fixtureSnapshot();
  render(<Pages snapshot={snapshot} path="/factory/issues/DEMO-104" />);
  const timeline = screen.getByRole("list");
  expect(within(timeline).getByText("Issue opened")).toBeDefined();
  expect(
    within(timeline).getByText("Review round 1: Changes requested"),
  ).toBeDefined();
  expect(within(timeline).getByText("Review round 2: Approved")).toBeDefined();
  expect(
    within(timeline).getByText("Reading list checks: Failed"),
  ).toBeDefined();
  expect(
    within(timeline).getByText("Reading list checks: Passed"),
  ).toBeDefined();
  expect(within(timeline).getByText("Merge queue: Entered")).toBeDefined();
  expect(
    screen
      .getByRole("link", { name: "PR #900004 merged" })
      .getAttribute("href"),
  ).toBe("https://github.com/jomcgi-org/homelab/pull/900004");
  expect(
    screen
      .getByRole("link", { name: "Session demo-build, turn 7" })
      .getAttribute("href"),
  ).toBe("/factory/sessions/demo-build#turn-7");
});

it("renders withheld turns, folded tool calls and inline diffs as text", async () => {
  const snapshot = await fixtureSnapshot();
  const { container } = render(
    <Pages snapshot={snapshot} path="/factory/sessions/demo-build" />,
  );
  expect(screen.getByText(withheldMessage)).toBeDefined();
  expect(container.querySelectorAll(".turn")).toHaveLength(28);
  expect(container.querySelectorAll("details.tool").length).toBeGreaterThan(3);
  expect(container.querySelector("details[open]")).toBeNull();
  expect(container.querySelector(".diff pre")?.textContent).toContain(
    "+const selected = savedItem;",
  );
  expect(container.textContent).toContain('<script>alert("fixture")</script>');
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("#turn-9")?.textContent).not.toContain(
    "Tool call",
  );
});

it("shows a malformed issue inline and keeps sibling issues visible", async () => {
  const files = fixtureFiles();
  await replaceObject(files, fixtureManifest().issues["DEMO-101"]!, {
    kind: "issue",
  });
  mockFiles(files);
  window.history.replaceState(null, "", "/factory/goals/reading-list");
  render(<App />);
  expect(
    await screen.findByText("Issue DEMO-101 not available."),
  ).toBeDefined();
  expect(
    screen.getByRole("link", { name: "DEMO-104: Open a saved reading list" }),
  ).toBeDefined();
});

it("keeps goal content available when the story object is missing", async () => {
  const snapshot = await fixtureSnapshot();
  render(
    <Pages
      snapshot={{ ...snapshot, story: null }}
      path="/factory/goals/reading-list"
    />,
  );
  expect(
    screen.getByRole("heading", {
      level: 1,
      name: "Return to the reading list",
    }),
  ).toBeDefined();
  expect(screen.getByText("Story statistics not available.")).toBeDefined();
  expect(
    screen.getByRole("link", { name: "DEMO-104: Open a saved reading list" }),
  ).toBeDefined();
});

it("shows an initial manifest failure with a route back to the story", async () => {
  mockFiles(new Map());
  render(<App />);
  expect(
    await screen.findByRole("heading", { name: "Factory story" }),
  ).toBeDefined();
  expect(screen.getByRole("link", { name: "Back to the story" })).toBeDefined();
  expect(screen.getByText(/Trying again every minute/).textContent).toContain(
    "Trying again every minute",
  );
});

it("navigates across pages, focuses the new content, and handles browser history", async () => {
  mockFiles();
  render(<App />);
  fireEvent.click(
    await screen.findByRole("link", { name: "Return to the reading list" }),
  );
  expect(window.location.pathname).toBe("/factory/goals/reading-list");
  fireEvent.click(
    screen.getByRole("link", { name: "DEMO-104: Open a saved reading list" }),
  );
  fireEvent.click(
    screen.getByRole("link", { name: "Session demo-build, turn 7" }),
  );
  expect(window.location.pathname + window.location.hash).toBe(
    "/factory/sessions/demo-build#turn-7",
  );
  expect(document.activeElement?.id).toBe("turn-7");
  fireEvent.click(screen.getByRole("link", { name: "Back to DEMO-104" }));
  expect(document.activeElement?.id).toBe("factory-content");
  act(() => {
    window.history.replaceState(null, "", "/factory/goals/reading-list");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  expect(
    screen.getByRole("heading", {
      level: 1,
      name: "Return to the reading list",
    }),
  ).toBeDefined();
});

it("polls every minute, keeps the last snapshot on failure, recovers and cleans up", async () => {
  const interval = vi.spyOn(window, "setInterval");
  const clear = vi.spyOn(window, "clearInterval");
  const files = fixtureFiles();
  mockFiles(files);
  const { unmount } = render(<App />);
  await screen.findByRole("heading", {
    level: 1,
    name: "A reading list that keeps your place",
  });
  const call = interval.mock.calls.find((args) => args[1] === 60_000)!;
  expect(call).toBeDefined();
  const tick = call[0] as () => void;
  files.delete("/data/factory/manifest.json");
  act(tick);
  expect(
    await screen.findByText(/Showing the last available snapshot/),
  ).toBeDefined();
  expect(
    screen.getByRole("heading", {
      level: 1,
      name: "A reading list that keeps your place",
    }),
  ).toBeDefined();
  files.set(
    "/data/factory/manifest.json",
    fixtureFiles().get("/data/factory/manifest.json")!,
  );
  const original = JSON.parse(
    files.get(`/data/factory/${fixtureManifest().story}`)!,
  );
  await replaceObject(files, fixtureManifest().story, {
    ...original,
    headline: "The next reading list chapter",
  });
  act(tick);
  expect(
    await screen.findByRole("heading", {
      level: 1,
      name: "The next reading list chapter",
    }),
  ).toBeDefined();
  await waitFor(() =>
    expect(
      screen.queryByText(/Showing the last available snapshot/),
    ).toBeNull(),
  );
  unmount();
  expect(clear).toHaveBeenCalled();
});

it("shows unavailable records for missing and malformed routes", async () => {
  const snapshot = await fixtureSnapshot();
  const { rerender } = render(
    <Pages snapshot={snapshot} path="/factory/issues/DEMO-999" />,
  );
  expect(
    screen.getByRole("heading", { name: "Record not available" }),
  ).toBeDefined();
  rerender(<Pages snapshot={snapshot} path="/factory/sessions/%ZZ" />);
  expect(
    screen.getByRole("heading", { name: "Record not available" }),
  ).toBeDefined();
});
