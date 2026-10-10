import { describe, expect, it } from "vitest";
import {
  knowledgeFields,
  projectionOf,
  scopeOf,
  selectedDetails,
  SPINE_FIELDS,
} from "./knowledge-fields.js";
import fixture from "./fixtures/reveal-projections.json";

describe("knowledge detail selection", () => {
  it("sends only selected detail fields and excludes metadata", () => {
    const entity = {
      id: "id",
      name: "Mara",
      grants: [],
      occupation: "Innkeeper",
      description: "DM_CANARY",
    };
    expect(
      selectedDetails(entity, ["occupation", "id", "missing"], " A clue "),
    ).toEqual({ occupation: "Innkeeper", clue: "A clue" });
  });
  it("renders only the granted partial projection", () => {
    expect(
      knowledgeFields({
        id: "id",
        name: "Mara",
        revealed_details: { occupation: "Innkeeper" },
      }),
    ).toEqual([["occupation", "Innkeeper"]]);
  });
});

describe("shared projection rules", () => {
  it("hides exactly the server spine fields", () => {
    expect(SPINE_FIELDS).toEqual(fixture.spine_fields);
    const spine = Object.fromEntries(SPINE_FIELDS.map((key) => [key, "x"]));
    expect(knowledgeFields({ ...spine, race: "Elf" })).toEqual([["race", "Elf"]]);
  });
  it("normalises an event entry and infers its scope", () => {
    for (const item of fixture.cases) {
      const projection = projectionOf({ entity: item.expected });
      expect(projection.name).toBe(item.expected.name);
      expect(scopeOf({ entity: item.expected })).toBe(
        item.name === "global_no_grant" ? "full" : item.name,
      );
    }
    expect(
      projectionOf({ entity_id: "e", name: "Mara", entity_type: "npc" }),
    ).toMatchObject({ id: "e", name: "Mara", entity_type: "npc" });
  });
});
