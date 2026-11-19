import snapshot from "./factory-outcomes.json";
import { FactoryOutcomes } from "./factory-outcomes";
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

const hub = readFileSync(
  "../../monolith/frontend/src/routes/public/app/llm-leaderboard/+page.svelte",
  "utf8",
);
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
const clickButton = (name: string, group?: string) =>
  fireEvent.click(
    (group
      ? within(screen.getByRole("group", { name: group }))
      : screen
    ).getByRole("button", { name }),
  );
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
  const seo = hub.match(
    /<Seo\s+title="([^"]+)"\s+description="([^"]+)"\s+path="([^"]+)"/,
  )!;
  const head = new DOMParser().parseFromString(
    readFileSync("llm-leaderboard.html", "utf8"),
    "text/html",
  );
  expect(head.title).toBe(seo[1]);
  const meta = (key: string) =>
    head
      .querySelector(
        `meta[${key.startsWith("og:") ? "property" : "name"}="${key}"]`,
      )
      ?.getAttribute("content");
  for (const key of ["description", "og:description", "twitter:description"])
    expect(meta(key)).toBe(seo[2]);
  for (const key of ["og:title", "twitter:title"])
    expect(meta(key)).toBe(seo[1]);
  expect(
    head.querySelector('link[rel="canonical"]')?.getAttribute("href"),
  ).toBe("https://jomcgi.dev/llm-leaderboard");
  expect(meta("og:url")).toBe("https://jomcgi.dev/llm-leaderboard");
  expect(meta("og:type")).toBe("website");
  expect(meta("og:site_name")).toBe("Joe McGinley");
  expect(meta("twitter:card")).toBe("summary");
  const redirects = readFileSync("public/_redirects", "utf8")
    .trim()
    .split("\n");
  for (const suffix of ["", "/*"])
    expect(redirects).toContain(
      `/app/llm-leaderboard${suffix} /llm-leaderboard 301`,
    );
});

it("copies the hub introduction, captions and source link verbatim", () => {
  const host = document.createElement("div");
  host.innerHTML = prerender("llm-leaderboard.html");
  const normalize = (text: string) => text.replace(/\s+/g, " ").trim();
  const lead = hub.match(/<div class="lead">([\s\S]*?)<\/div>/)![1]!;
  const original = new DOMParser().parseFromString(lead, "text/html");
  for (const tag of ["h1", "p"]) {
    expect(normalize(host.querySelector(".lead > " + tag)!.textContent!)).toBe(
      normalize(original.querySelector(tag)!.textContent!),
    );
  }
  const captions = [...hub.matchAll(/<p class="caption">([^<{]+)<\/p>/g)].map(
    (match) => normalize(match[1]!),
  );
  expect(captions.length).toBeGreaterThan(0);
  const rendered = [...host.querySelectorAll("p.caption")].map((node) =>
    normalize(node.textContent!),
  );
  for (const caption of captions) expect(rendered).toContain(caption);
  const sourceLink = hub.match(
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
    clickButton(preset);
    if (preset !== "Clear all") clickButton("Tokens per task", "X axis metric");
    expect(container.querySelector("[style]")).toBeNull();
    const result = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.violations).toEqual([]);
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
  clickButton("Self-hosted");
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
  clickButton("Top picks");
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
  clickButton("Clear all");
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
  clickButton("All");
  expect(modelIds(rankedTable())).toHaveLength(leaderboard.models.length);
  clickButton("Tokens per task", "X axis metric");
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
  const index = () =>
    within(screen.getByRole("table", { name: "Agent index" }));
  expect(index().getByText("0.800")).toBeTruthy();
  clickButton("reviewer");
  expect(index().getByText("0.600")).toBeTruthy();
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

it("keeps only public aggregate fields in the committed factory snapshot", () => {
  type Row = NonNullable<Parameters<typeof FactoryOutcomes>[0]["arms"]>[number];
  for (const row of snapshot as Row[]) {
    expect(Object.keys(row).sort()).toEqual([
      "arm",
      "median_minutes",
      "median_rounds",
      "merge_rate",
      "n",
      "revert_rate",
    ]);
    expect(typeof row.arm).toBe("string");
    expect(Number.isSafeInteger(row.n) && row.n > 0).toBe(true);
    for (const median of [row.median_minutes, row.median_rounds]) {
      expect(median === null || Number.isFinite(median)).toBe(true);
    }
    for (const rate of [
      row.merge_rate,
      ...(row.revert_rate === null ? [] : [row.revert_rate]),
    ]) {
      expect(Object.keys(rate).sort()).toEqual(["high", "low", "value"]);
      expect(Object.values(rate).every(Number.isFinite)).toBe(true);
    }
  }
});

it("marks small factory samples and shows merge uncertainty", () => {
  const arm = {
    arm: "control",
    merge_rate: { value: 0.5, low: 0.2, high: 0.8 },
    revert_rate: null,
    median_minutes: null,
    median_rounds: 1,
  };
  render(
    <FactoryOutcomes
      arms={[
        { ...arm, n: 9 },
        { ...arm, arm: "trial", n: 10 },
      ]}
    />,
  );
  expect(screen.getAllByText(/insufficient data/)).toHaveLength(1);
  expect(screen.getByText("9 · insufficient data")).toBeTruthy();
  expect(screen.getAllByText("50.0% (20.0%–80.0%)")).toHaveLength(2);
});
