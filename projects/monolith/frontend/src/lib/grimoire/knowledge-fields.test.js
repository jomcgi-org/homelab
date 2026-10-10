import { describe, expect, it } from "vitest";
import { knowledgeFields, selectedDetails } from "./knowledge-fields.js";

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
