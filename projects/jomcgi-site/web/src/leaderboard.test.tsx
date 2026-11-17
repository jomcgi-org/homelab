import axe from "axe-core";
import { readFileSync } from "node:fs";
import { act } from "react";
import { hydrateRoot } from "react-dom/client";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { LlmLeaderboardPage } from "./llm-leaderboard-page";
import { LeaderboardContent } from "./leaderboard-content";
import { leaderboard } from "./leaderboard-data";
import { Scatter } from "./leaderboard-charts";
import {
  cellState,
  orderedTasks,
  presetIds,
  rank,
  serializeSelection,
  shortName,
} from "./leaderboard-model";
import { render as prerender } from "./prerender";
import { Root } from "./root";

const modelIds = (table: HTMLElement) =>
  [...table.querySelectorAll("tbody tr")].map((row) =>
    row.getAttribute("data-model"),
  );
const rankedTable = () => screen.getByRole("table", { name: "Ranked models" });
function renderPage() {
  return render(
    <Root>
      <LeaderboardContent data={leaderboard} />
    </Root>,
  );
}
afterEach(() => window.history.replaceState(null, "", "/"));

it("pre-renders the shared snapshot, charts, task descriptions and exact ranked readings", () => {
  const host = document.createElement("div");
  host.innerHTML = prerender("llm-leaderboard.html");
  expect(host.querySelector("[style]")).toBeNull();
  const models = rank(leaderboard.models).filter((m) =>
    presetIds(leaderboard.models, "default").has(m.id),
  );
  const table = within(host).getByRole("table", { name: "Ranked models" });
  expect(modelIds(table)).toEqual(models.map((m) => m.id));
  const matrix = within(host).getByRole("table", { name: "Per-task results" });
  for (const m of models) {
    const row = [...matrix.querySelectorAll("tbody tr")].find(
      (row) => row.getAttribute("data-model") === m.id,
    )!;
    expect(
      [...row.querySelectorAll("i.cell")].map((cell) => cell.classList[1]),
    ).toEqual(orderedTasks(leaderboard.tasks).map((t) => cellState(m, t.id)));
  }
  expect(host.querySelectorAll(".bars svg rect")).not.toHaveLength(0);
  expect(
    within(host).getByRole("region", { name: "Hard-task pass chart" }).tabIndex,
  ).toBe(0);
  expect(
    within(host).getByRole("img", {
      name: "Hard-task pass against mean price per task",
    }),
  ).toBeTruthy();
  for (const task of orderedTasks(leaderboard.tasks))
    expect(host.querySelector(`#task-${task.no}`)?.textContent).toContain(
      task.blurb,
    );
});

it("keeps the hub title, description and preview metadata", () => {
  const source = readFileSync(
    "../../monolith/frontend/src/routes/public/app/llm-leaderboard/+page.svelte",
    "utf8",
  );
  const seo = source.match(
    /<Seo\s+title="([^"]+)"\s+description="([^"]+)"\s+path="([^"]+)"/,
  )!;
  const head = new DOMParser().parseFromString(
    readFileSync("llm-leaderboard.html", "utf8"),
    "text/html",
  );
  expect(head.title).toBe(seo[1]);
  for (const selector of [
    'meta[name="description"]',
    'meta[property="og:description"]',
    'meta[name="twitter:description"]',
  ])
    expect(head.querySelector(selector)?.getAttribute("content")).toBe(seo[2]);
  for (const selector of [
    'meta[property="og:title"]',
    'meta[name="twitter:title"]',
  ])
    expect(head.querySelector(selector)?.getAttribute("content")).toBe(seo[1]);
  expect(
    head.querySelector('link[rel="canonical"]')?.getAttribute("href"),
  ).toBe(`https://jomcgi.dev${seo[3]}`);
  expect(
    head.querySelector('meta[property="og:url"]')?.getAttribute("content"),
  ).toBe(`https://jomcgi.dev${seo[3]}`);
  expect(
    head.querySelector('meta[property="og:type"]')?.getAttribute("content"),
  ).toBe("website");
  expect(
    head
      .querySelector('meta[property="og:site_name"]')
      ?.getAttribute("content"),
  ).toBe("Joe McGinley");
  expect(
    head.querySelector('meta[name="twitter:card"]')?.getAttribute("content"),
  ).toBe("summary");
  expect(
    readFileSync("public/_redirects", "utf8").trim().split("\n"),
  ).toContain("/app/llm-leaderboard /llm-leaderboard 301");
  expect(
    readFileSync("public/_redirects", "utf8").trim().split("\n"),
  ).toContain("/app/llm-leaderboard/* /llm-leaderboard 301");
});

it("copies the hub introduction, captions and source link verbatim", () => {
  const source = readFileSync(
    "../../monolith/frontend/src/routes/public/app/llm-leaderboard/+page.svelte",
    "utf8",
  );
  const host = document.createElement("div");
  host.innerHTML = prerender("llm-leaderboard.html");
  const normalize = (text: string) => text.replace(/\s+/g, " ").trim();
  const lead = source.match(/<div class="lead">([\s\S]*?)<\/div>/)![1]!;
  const original = new DOMParser().parseFromString(lead, "text/html");
  for (const tag of ["h1", "p"]) {
    expect(normalize(host.querySelector(".lead " + tag)!.textContent!)).toBe(
      normalize(original.querySelector(tag)!.textContent!),
    );
  }
  const captions = [
    ...source.matchAll(/<p class="caption">([^<{]+)<\/p>/g),
  ].map((match) => normalize(match[1]!));
  expect(captions.length).toBeGreaterThan(0);
  const rendered = [...host.querySelectorAll("p.caption")].map((node) =>
    normalize(node.textContent!),
  );
  for (const caption of captions) expect(rendered).toContain(caption);
  const sourceLink = source.match(
    /<footer class="caption method">[\s\S]*?<a href="([^"]+)"/,
  )![1]!;
  expect(host.querySelector(".method a")?.getAttribute("href")).toBe(
    sourceLink,
  );
});

it.each(["All", "Self-hosted", "Clear all"])(
  "has no axe violations or inline styles after selecting %s and changing axes",
  async (preset) => {
    const { container } = renderPage();
    fireEvent.click(screen.getByRole("button", { name: preset }));
    if (preset !== "Clear all") {
      fireEvent.click(
        within(screen.getByRole("group", { name: "X axis metric" })).getByRole(
          "button",
          { name: "Tokens per task" },
        ),
      );
    }
    expect(container.querySelector("[style]")).toBeNull();
    const result = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(
      result.violations.map(({ id, nodes }) => ({
        id,
        targets: nodes.map((node) => node.target),
      })),
    ).toEqual([]);
  },
  // All models adds hundreds of matrix cells; axe can exceed the default 5s
  // when the rest of the suite runs concurrently on shared CI workers.
  15_000,
);

it("hydrates linked selections without a mismatch and keeps unrelated query parameters", async () => {
  const m = leaderboard.models[0]!;
  window.history.replaceState(
    { kept: true },
    "",
    `/llm-leaderboard?keep=yes&m=${encodeURIComponent(m.id)}#task-1`,
  );
  const host = document.createElement("div");
  host.innerHTML = prerender("llm-leaderboard.html");
  document.body.append(host);
  const errors: unknown[] = [];
  let root: ReturnType<typeof hydrateRoot>;
  await act(async () => {
    root = hydrateRoot(
      host,
      <Root>
        <LlmLeaderboardPage />
      </Root>,
      { onRecoverableError: (error) => errors.push(error) },
    );
  });
  expect(errors).toEqual([]);
  await waitFor(() => expect(modelIds(rankedTable())).toEqual([m.id]));
  const historyLength = window.history.length;
  fireEvent.click(screen.getByRole("button", { name: "Self-hosted" }));
  expect(new URL(window.location.href).searchParams.get("m")).toBe(
    serializeSelection(
      leaderboard.models,
      presetIds(leaderboard.models, "self"),
    ),
  );
  expect(window.location.search).toContain("keep=yes");
  expect(window.location.hash).toBe("#task-1");
  expect(window.history.length).toBe(historyLength);
  expect(window.history.state).toEqual({ kept: true });
  fireEvent.click(screen.getByRole("button", { name: "Top picks" }));
  expect(new URL(window.location.href).searchParams.has("m")).toBe(false);
  window.history.replaceState(
    null,
    "",
    `/llm-leaderboard?m=${encodeURIComponent(m.id)}`,
  );
  act(() => window.dispatchEvent(new PopStateEvent("popstate")));
  expect(modelIds(rankedTable())).toEqual([m.id]);
  await act(async () => root!.unmount());
  host.remove();
});

it("searches, selects, clears and changes chart axes and table order", () => {
  renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
  expect(screen.queryByRole("table", { name: "Ranked models" })).toBeNull();
  expect(
    screen.getByText("No models selected. Pick a preset or search above."),
  ).toBeTruthy();
  const search = screen.getByRole("searchbox", { name: "Search models" });
  fireEvent.change(search, { target: { value: "qwen" } });
  expect(screen.getByText("Choose models").closest("details")?.open).toBe(true);
  fireEvent.keyDown(search, { key: "Enter" });
  expect(modelIds(rankedTable())).toEqual(
    rank(leaderboard.models)
      .filter((m) => `${shortName(m)} ${m.id}`.toLowerCase().includes("qwen"))
      .map((m) => m.id),
  );
  const first = screen.getAllByRole("checkbox")[0]!;
  fireEvent.click(first);
  expect((first as HTMLInputElement).checked).toBe(false);
  fireEvent.keyDown(search, { key: "Escape" });
  expect((search as HTMLInputElement).value).toBe("");
  fireEvent.change(search, { target: { value: "no-such-model" } });
  expect(screen.getByText("No model matches “no-such-model”.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  expect(modelIds(rankedTable())).toHaveLength(leaderboard.models.length);
  fireEvent.click(
    within(screen.getByRole("group", { name: "X axis metric" })).getByRole(
      "button",
      { name: "Tokens per task" },
    ),
  );
  expect(
    screen.getByRole("img", {
      name: "Hard-task pass against mean prompt + completion tokens per task",
    }),
  ).toBeTruthy();
  const sort = within(rankedTable()).getByRole("button", { name: "model" });
  fireEvent.click(sort);
  const ascending = [...leaderboard.models]
    .sort((a, b) => shortName(a).localeCompare(shortName(b)))
    .map((m) => m.id);
  expect(modelIds(rankedTable())).toEqual(ascending);
  fireEvent.click(sort);
  expect(modelIds(rankedTable())).toEqual(ascending.reverse());
});

it("keeps future per-role agent index data interactive", () => {
  const m = leaderboard.models[0]!;
  window.history.replaceState(null, "", `/?m=${encodeURIComponent(m.id)}`);
  render(
    <Root>
      <LeaderboardContent
        data={{
          ...leaderboard,
          index: {
            roles: { coder: {}, reviewer: {} },
            min_n: 5,
            models: {
              [m.id]: {
                low_n: ["tools"],
                roles: {
                  coder: { index: 0.8, ci: [0.7, 0.9], coverage: 1 },
                  reviewer: { index: 0.6 },
                },
              },
            },
            picks: { coder: { best: m.id } },
          },
        }}
      />
    </Root>,
  );
  expect(
    within(screen.getByRole("table", { name: "Agent index" })).getByText(
      "0.800",
    ),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "reviewer" }));
  expect(
    within(screen.getByRole("table", { name: "Agent index" })).getByText(
      "0.600",
    ),
  ).toBeTruthy();
});

// A log plot with only zero or missing values used to loop forever generating
// ticks from -Infinity. This also exercises the zero lane without paid models.
describe.each([
  { models: [] },
  { models: [{ ...leaderboard.models[0]!, cost_usd: 0 }] },
])("empty log domain", ({ models }) => {
  it("renders finite geometry", () => {
    const { container } = render(
      <Scatter models={models} hot={null} onHover={() => {}} />,
    );
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
  });
});
