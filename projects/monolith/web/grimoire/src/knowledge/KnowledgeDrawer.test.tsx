import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import fixture from "../lib/fixtures/reveal-projections.json";
import { ids } from "../test/fixtures";
import { KnowledgeDrawer } from "./KnowledgeDrawer";
import { mount } from "./mount.testing";

const ENTITY = "GET /campaigns/{campaign_id}/entities/{entity_id}";
const partial = fixture.cases[1]!.expected;

describe("knowledge drawer", () => {
  it("reads nothing while closed", () => {
    const { backend } = mount(
      <KnowledgeDrawer
        campaignId={ids.owned}
        entityId={null}
        onClose={vi.fn()}
      />,
      { [ENTITY]: partial },
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(backend.calls).toHaveLength(0);
  });

  it("shows the viewer's projection and a link to its page", async () => {
    const onClose = vi.fn();
    const { backend, user } = mount(
      <KnowledgeDrawer
        campaignId={ids.owned}
        entityId={partial.id}
        onClose={onClose}
      />,
      { [ENTITY]: partial },
    );
    const dialog = await screen.findByRole("dialog");
    expect(dialog.getAttribute("aria-label")).toBe("Knowledge detail");
    expect(
      await within(dialog).findByRole("heading", { name: partial.name }),
    ).toBeTruthy();
    expect(backend.callsTo(ENTITY)[0]?.params.entity_id).toBe(partial.id);
    expect(
      within(dialog)
        .getAllByRole("term")
        .map((term) => term.textContent),
    ).toEqual(["disposition", "clue"]);
    expect(
      within(dialog)
        .getByRole("link", { name: "Open knowledge page" })
        .getAttribute("href"),
    ).toBe(`/campaigns/${ids.owned}/entities/${partial.id}`);
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("hides an entity whose grant is retracted while it is open", async () => {
    const { backend } = mount(
      <KnowledgeDrawer
        campaignId={ids.owned}
        entityId={partial.id}
        onClose={vi.fn()}
      />,
      { [ENTITY]: partial },
    );
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("heading", { name: partial.name });
    backend.set({ [ENTITY]: { status: 404, detail: "entity not found" } });
    await waitFor(
      () =>
        expect(
          within(dialog).getByText(
            "This knowledge is no longer available to you.",
          ),
        ).toBeTruthy(),
      { timeout: 6000 },
    );
    expect(dialog.textContent).not.toContain(partial.name);
    expect(dialog.textContent).not.toContain("Seen near the docks");
  }, 10_000);
});
