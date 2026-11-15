import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import fixture from "../lib/fixtures/reveal-projections.json";
import { RevealProjection, type RevealedKnowledge } from "./RevealProjection";

const terms = () =>
  screen.queryAllByRole("term").map((term) => term.textContent);

describe("a reveal in the feed", () => {
  // The same golden cases the backend's project_entity() test asserts.
  for (const item of fixture.cases) {
    it(`shows exactly the fields of a ${item.name} projection`, () => {
      const scope = item.grant?.grant_scope ?? "full";
      const { container } = render(
        <RevealProjection
          knowledge={{
            entity_id: item.expected.id,
            name: item.expected.name,
            entity_type: item.expected.entity_type,
            grant_scope: scope,
            // A name-only reveal event carries no projection at all.
            ...(scope !== "name_only" && { entity: item.expected }),
          }}
        />,
      );
      expect(terms().sort()).toEqual([...item.visible_fields].sort());
      expect(container.textContent).toContain(item.expected.name);
      for (const hidden of item.hidden_values ?? [])
        expect(container.textContent).not.toContain(hidden);
      expect(screen.queryByText("You recognize this name.") !== null).toBe(
        scope === "name_only",
      );
    });
  }

  it("renders markdown in a text field as formatting, not markup", () => {
    const { container } = render(
      <RevealProjection
        knowledge={{
          projection: fixture.cases[0]!.expected,
          grant_scope: "full",
        }}
      />,
    );
    expect(container.textContent).toContain(
      "A tall barbarian with a scarred jaw and a hidden past.",
    );
    expect(container.querySelector("strong + *, dd strong")?.textContent).toBe(
      "tall",
    );
  });

  it("shows nothing beyond the name when the scope is name only", () => {
    const leaked: RevealedKnowledge = {
      entity_id: fixture.entities.private.id,
      name: fixture.entities.private.name,
      entity_type: "npc",
      grant_scope: "name_only",
      entity: { ...fixture.entities.private, ...fixture.details.private },
    };
    const { container } = render(<RevealProjection knowledge={leaked} />);
    expect(terms()).toEqual([]);
    expect(container.textContent).not.toContain("Aasimar");
  });

  it("never lists identity or bookkeeping columns as knowledge", () => {
    render(
      <RevealProjection
        knowledge={{
          projection: fixture.cases[3]!.expected,
          grant_scope: "full",
        }}
      />,
    );
    for (const field of fixture.spine_fields)
      expect(terms()).not.toContain(field.replaceAll("_", " "));
  });
});
