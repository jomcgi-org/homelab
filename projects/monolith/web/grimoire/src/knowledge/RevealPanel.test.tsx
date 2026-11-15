import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { EntityProjection } from "../api/knowledge";
import type { BulkGrantRequest } from "../api/schema";
import fixture from "../lib/fixtures/reveal-projections.json";
import { chooseOption, findToast } from "../test/app";
import type { Routes } from "../test/backend";
import { ids } from "../test/fixtures";
import { mount } from "./mount.testing";
import { RevealPanel } from "./RevealPanel";

const aria = { id: fixture.viewer, character_name: "Aria" };
const bram = { id: ids.spare, character_name: "Bram" };
const spine = fixture.entities.private;
const dmEntity: EntityProjection = {
  ...spine,
  ...fixture.details.private,
  grants: [],
};
const bramGrant = {
  id: "7b000000-0000-4000-8000-000000000001",
  player_character_id: bram.id,
  grant_scope: "full" as const,
  revealed_details: null,
};

const PREVIEW = "POST /campaigns/{campaign_id}/grants/preview";
const BULK = "POST /campaigns/{campaign_id}/grants/bulk";
const REVOKE = "DELETE /campaigns/{campaign_id}/grants/{grant_id}";
const LIST = "GET /campaigns/{campaign_id}/entities";

function routes(
  projection: object = fixture.cases[0]!.expected,
  entity: EntityProjection = dmEntity,
): Routes {
  return {
    [LIST]: { items: [spine], total: 1, next_cursor: null },
    "GET /campaigns/{campaign_id}/entities/{entity_id}": entity,
    [PREVIEW]: (call) =>
      (call.body as BulkGrantRequest).grants.map((grant) => ({
        player_character_id: grant.player_character_id,
        projection,
      })),
    [BULK]: [],
    [REVOKE]: undefined,
  };
}

async function open(using: Routes = routes()) {
  const mounted = mount(
    <RevealPanel campaignId={ids.owned} characters={[aria, bram]} />,
    using,
  );
  await mounted.user.click(
    screen.getByRole("button", { name: "Reveal knowledge" }),
  );
  return { ...mounted, dialog: await screen.findByRole("dialog") };
}

/** Opens the drawer and picks the fixture entity from the search results. */
async function openEntity(using: Routes = routes()) {
  const opened = await open(using);
  const box = within(opened.dialog).getByRole("combobox", {
    name: "Find knowledge",
  });
  await opened.user.click(box);
  const option = await within(opened.dialog).findByRole("option", {
    name: new RegExp(spine.name),
  });
  // The menu commits on pointer-up, which a synthetic click does not send.
  fireEvent.pointerDown(option);
  fireEvent.pointerUp(option);
  fireEvent.click(option);
  await within(opened.dialog).findByRole("heading", { name: spine.name });
  return opened;
}

const button = (dialog: HTMLElement, name: string) =>
  within(dialog).getByRole("button", { name });
const previewRegion = (dialog: HTMLElement) =>
  within(dialog).findByRole("region", { name: "Recipient previews" });

describe("reveal drawer", () => {
  it("loads nothing until it is opened", async () => {
    const { backend } = mount(
      <RevealPanel campaignId={ids.owned} characters={[aria]} />,
      routes(),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByLabelText("Find knowledge")).toBeNull();
    expect(backend.calls).toHaveLength(0);
  });

  it("opens a named drawer holding the editor", async () => {
    const { dialog } = await open();
    expect(dialog.getAttribute("aria-label")).toBe("Reveal to your players");
    expect(
      within(dialog).getByRole("heading", { name: "Reveal to your players" }),
    ).toBeTruthy();
    expect(within(dialog).getByLabelText("Find knowledge")).toBeTruthy();
  });

  it("keeps the chosen entity and draft across a close and reopen", async () => {
    const { user, dialog } = await openEntity();
    await user.click(within(dialog).getByRole("checkbox", { name: "Aria" }));
    await user.click(button(dialog, "Close"));
    await user.click(screen.getByRole("button", { name: "Reveal knowledge" }));
    const reopened = await screen.findByRole("dialog");
    expect(
      within(reopened).getByRole("heading", { name: spine.name }),
    ).toBeTruthy();
    expect(
      within(reopened).getByRole<HTMLInputElement>("checkbox", {
        name: "Aria",
      }).checked,
    ).toBe(true);
  });

  it("hides what a chosen character already knows from the search", async () => {
    const { user, backend, dialog } = await open();
    await chooseOption(dialog, "Hide knowledge already granted to", "Bram");
    await user.click(
      within(dialog).getByRole("combobox", { name: "Find knowledge" }),
    );
    await waitFor(() =>
      expect(backend.callsTo(LIST).at(-1)?.query.get("not_granted_to")).toBe(
        bram.id,
      ),
    );
    expect(backend.callsTo(LIST).at(-1)?.query.get("limit")).toBe("30");
  });

  it("waits for an IME composition to finish before searching", async () => {
    const { backend, dialog } = await open();
    const box = within(dialog).getByRole("combobox", {
      name: "Find knowledge",
    });
    fireEvent.compositionStart(box);
    fireEvent.change(box, { target: { value: "ゆき" } });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(backend.callsTo(LIST)).toHaveLength(0);
    fireEvent.compositionEnd(box);
    await waitFor(() =>
      expect(backend.callsTo(LIST).at(-1)?.query.get("q")).toBe("ゆき"),
    );
  });
});

describe("reveal preview matches the server projection", () => {
  const choices = {
    full: "Full knowledge",
    partial: "Selected detail",
    name_only: "Name only",
    global_no_grant: "Full knowledge",
  };
  const plain = (value: string) => value.replaceAll("**", "");

  for (const item of fixture.cases) {
    it(`renders the ${item.name} projection exactly`, async () => {
      const { user, dialog } = await openEntity(routes(item.expected));
      await user.click(within(dialog).getByRole("checkbox", { name: "Aria" }));
      await chooseOption(
        dialog,
        "Knowledge scope",
        choices[item.name as keyof typeof choices],
      );
      if (item.name === "partial")
        await user.click(
          within(dialog).getByRole("checkbox", { name: "disposition" }),
        );
      await user.click(button(dialog, "Preview knowledge"));

      const preview = await previewRegion(dialog);
      expect(
        within(preview)
          .queryAllByRole("term")
          .map((term) => term.textContent)
          .sort(),
      ).toEqual([...item.visible_fields].sort());
      const expected = item.expected as EntityProjection;
      const shown = (expected.revealed_details ?? expected) as Record<
        string,
        string
      >;
      for (const key of item.visible_fields)
        expect(preview.textContent).toContain(plain(shown[key]!));
      expect(preview.textContent).toContain(expected.name);
      expect(preview.textContent).toContain(expected.entity_type);
      for (const hidden of item.hidden_values ?? [])
        expect(preview.textContent).not.toContain(hidden);
      expect(
        within(preview).queryByText("You recognize this name.") !== null,
      ).toBe(item.name === "name_only");
    });
  }
});

describe("revealing", () => {
  const grantsOf = (body: unknown) => (body as BulkGrantRequest).grants;

  it("offers sharing only for the request that was previewed", async () => {
    const { user, backend, dialog } = await openEntity();
    expect(button(dialog, "Preview knowledge")).toHaveProperty(
      "disabled",
      true,
    );
    await user.click(within(dialog).getByRole("checkbox", { name: "Aria" }));
    expect(
      within(dialog).queryByRole("button", { name: "Share knowledge" }),
    ).toBeNull();
    await user.click(button(dialog, "Preview knowledge"));
    await previewRegion(dialog);
    expect(button(dialog, "Share knowledge")).toBeTruthy();

    await chooseOption(dialog, "Knowledge scope", "Full knowledge");
    expect(
      within(dialog).queryByRole("region", { name: "Recipient previews" }),
    ).toBeNull();
    expect(
      within(dialog).queryByRole("button", { name: "Share knowledge" }),
    ).toBeNull();
    expect(backend.callsTo(BULK)).toHaveLength(0);
  });

  it("shares exactly what was previewed and refreshes the feed", async () => {
    const { user, backend, client, dialog } = await openEntity();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await user.click(within(dialog).getByRole("checkbox", { name: "Aria" }));
    await chooseOption(dialog, "Knowledge scope", "Full knowledge");
    await user.click(button(dialog, "Preview knowledge"));
    await previewRegion(dialog);
    await user.click(button(dialog, "Share knowledge"));

    expect(await findToast("Knowledge shared.")).toBeTruthy();
    const expected = {
      grants: [
        {
          entity_id: spine.id,
          player_character_id: aria.id,
          grant_scope: "full",
          revealed_details: null,
        },
      ],
    };
    expect(backend.callsTo(PREVIEW)[0]?.body).toEqual(expected);
    expect(backend.callsTo(BULK)[0]?.body).toEqual(expected);
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["campaign", ids.owned, "session"],
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["campaign", ids.owned, "knowledge"],
    });
  });

  it("sends only the picked fields of a partial reveal", async () => {
    const { user, backend, dialog } = await openEntity();
    await user.click(within(dialog).getByRole("checkbox", { name: "Aria" }));
    await chooseOption(dialog, "Knowledge scope", "Selected detail");
    expect(button(dialog, "Preview knowledge")).toHaveProperty(
      "disabled",
      true,
    );
    await user.click(
      within(dialog).getByRole("checkbox", { name: "occupation" }),
    );
    await user.click(button(dialog, "Preview knowledge"));
    await previewRegion(dialog);
    await user.click(button(dialog, "Share knowledge"));
    await waitFor(() => expect(backend.callsTo(BULK)).toHaveLength(1));
    expect(grantsOf(backend.callsTo(BULK)[0]!.body)[0]).toMatchObject({
      grant_scope: "partial",
      revealed_details: { occupation: fixture.details.private.occupation },
    });
    expect(
      Object.keys(
        grantsOf(backend.callsTo(BULK)[0]!.body)[0]!.revealed_details!,
      ),
    ).toEqual(["occupation"]);
  });

  it("adds the trimmed clue and nothing else", async () => {
    const { user, backend, dialog } = await openEntity();
    await user.click(within(dialog).getByRole("checkbox", { name: "Aria" }));
    await chooseOption(dialog, "Knowledge scope", "Selected detail");
    await user.click(within(dialog).getByRole("checkbox", { name: "race" }));
    await user.type(
      within(dialog).getByLabelText("Detail to share"),
      " Seen at night ",
    );
    await user.click(button(dialog, "Preview knowledge"));
    await previewRegion(dialog);
    await user.click(button(dialog, "Share knowledge"));
    await waitFor(() => expect(backend.callsTo(BULK)).toHaveLength(1));
    expect(
      grantsOf(backend.callsTo(BULK)[0]!.body)[0]!.revealed_details,
    ).toEqual({ race: fixture.details.private.race, clue: "Seen at night" });
  });

  it("shows the backend's refusal and keeps the draft", async () => {
    const { user, dialog } = await openEntity({
      ...routes(),
      [BULK]: {
        status: 409,
        detail: "grant already exists for this entity and character",
      },
    });
    await user.click(within(dialog).getByRole("checkbox", { name: "Aria" }));
    await user.click(button(dialog, "Preview knowledge"));
    await previewRegion(dialog);
    await user.click(button(dialog, "Share knowledge"));
    expect(
      await findToast("grant already exists for this entity and character"),
    ).toBeTruthy();
    expect(button(dialog, "Share knowledge")).toBeTruthy();
  });
});

describe("retracting", () => {
  const granted = () =>
    openEntity(routes(undefined, { ...dmEntity, grants: [bramGrant] }));

  it("cannot share again with a character who holds a grant", async () => {
    const { dialog } = await granted();
    const box = within(dialog).getByRole<HTMLInputElement>("checkbox", {
      name: "Bram",
    });
    expect(box.disabled || box.getAttribute("aria-disabled") === "true").toBe(
      true,
    );
    expect(within(dialog).getByText("Already shared: Full")).toBeTruthy();
  });

  it("retracts with a notice by default", async () => {
    const { user, backend, dialog } = await granted();
    await user.click(button(dialog, "Retract from Bram"));
    expect(await findToast("Knowledge retracted.")).toBeTruthy();
    const [call] = backend.callsTo(REVOKE);
    expect(call?.params.grant_id).toBe(bramGrant.id);
    expect(call?.query.get("silent")).toBe("false");
  });

  it("retracts silently when asked", async () => {
    const { user, backend, dialog } = await granted();
    await user.click(
      within(dialog).getByRole("checkbox", { name: "Retract silently" }),
    );
    await user.click(button(dialog, "Retract from Bram"));
    await waitFor(() => expect(backend.callsTo(REVOKE)).toHaveLength(1));
    expect(backend.callsTo(REVOKE)[0]?.query.get("silent")).toBe("true");
  });

  it("offers no retraction when nobody holds a grant", async () => {
    const { dialog } = await openEntity();
    expect(
      within(dialog).queryByRole("checkbox", { name: "Retract silently" }),
    ).toBeNull();
  });
});
