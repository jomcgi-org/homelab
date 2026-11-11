import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import fixture from "../../lib/fixtures/reveal-projections.json";
import type { Routes } from "../../test/backend";
import { openApp } from "../../test/app";
import { ids, lobby } from "../../test/fixtures";

const ENTITY = "GET /campaigns/{campaign_id}/entities/{entity_id}";
const base: Routes = { "GET /lobby": lobby() };
const path = (entityId: string) =>
  `/campaigns/${ids.owned}/entities/${entityId}`;
const terms = () => screen.queryAllByRole("term").map((row) => row.textContent);

describe("Entity", () => {
  it("shows a full projection under the entity's name", async () => {
    const full = fixture.cases[0]!.expected;
    const { backend } = openApp({ ...base, [ENTITY]: full }, path(full.id));
    expect(
      await screen.findByRole("heading", { level: 1, name: full.name }),
    ).toBeTruthy();
    expect(screen.getByText("npc")).toBeTruthy();
    expect(terms()).toEqual([
      "race",
      "occupation",
      "disposition",
      "description",
    ]);
    expect(backend.callsTo(ENTITY)[0]?.params).toEqual({
      campaign_id: ids.owned,
      entity_id: full.id,
    });
    expect(
      screen
        .getByRole("link", { name: "Back to the table" })
        .getAttribute("href"),
    ).toBe(`/campaigns/${ids.owned}/session`);
  });

  it("shows only the revealed details of a partial projection", async () => {
    const partial = fixture.cases[1]!;
    openApp({ ...base, [ENTITY]: partial.expected }, path(partial.expected.id));
    await screen.findByRole("heading", {
      level: 1,
      name: partial.expected.name,
    });
    expect(terms()).toEqual(["disposition", "clue"]);
    for (const hidden of partial.hidden_values ?? [])
      expect(document.body.textContent).not.toContain(hidden);
  });

  it("answers a malformed id without asking the backend", async () => {
    const { backend } = openApp(base, path("not-an-entity"));
    expect(
      await screen.findByRole("heading", {
        name: "Knowledge entry not found.",
      }),
    ).toBeTruthy();
    expect(backend.callsTo(ENTITY)).toHaveLength(0);
  });

  it("does not say whether a refused entry exists", async () => {
    const id = fixture.entities.private.id;
    openApp(
      { ...base, [ENTITY]: { status: 404, detail: "entity not found" } },
      path(id),
    );
    expect(
      await screen.findByRole("heading", {
        name: "This knowledge entry is not available to you.",
      }),
    ).toBeTruthy();
    expect(screen.queryAllByRole("term")).toHaveLength(0);
  });
});
