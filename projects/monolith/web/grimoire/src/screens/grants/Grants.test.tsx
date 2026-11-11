import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { GrantView } from "../../api/schema";
import type { Routes } from "../../test/backend";
import { chooseOption, findToast, openApp } from "../../test/app";
import { campaigns, characters, ids, lobby } from "../../test/fixtures";
import {
  dmEntities,
  entityIds,
  keeperGrant,
  knowledgeRoutes,
} from "../../test/fixtures/knowledge";
import { activeSession } from "../../test/fixtures/session";

const path = `/campaigns/${ids.owned}/grants`;
const brannoch = characters[0]!;
const wren = {
  ...brannoch,
  id: "dddddddd-dddd-4ddd-8ddd-ddddddddddd2",
  character_name: "Wren Ashby",
};
const keyGrant: GrantView = {
  ...keeperGrant,
  id: "72222222-2222-4222-8222-222222222222",
  entity_id: entityIds.key,
  player_character_id: wren.id,
  grant_scope: "name_only",
  revealed_details: null,
  granted_in_session: null,
};

const PREVIEW = "POST /campaigns/{campaign_id}/grants/preview";
const BULK = "POST /campaigns/{campaign_id}/grants/bulk";
const PATCH = "PATCH /campaigns/{campaign_id}/grants/{grant_id}";
const REVOKE = "DELETE /campaigns/{campaign_id}/grants/{grant_id}";
const LIST = "GET /campaigns/{campaign_id}/entities";

const dm: Routes = {
  ...knowledgeRoutes,
  "GET /lobby": lobby(),
  "GET /campaigns/{campaign_id}/characters": [brannoch, wren],
  "GET /campaigns/{campaign_id}/sessions": [activeSession],
  "GET /campaigns/{campaign_id}/grants": [keeperGrant, keyGrant],
};

const matrix = () => screen.findByRole("list", { name: "Knowledge grants" });
const cell = (list: HTMLElement, name: string) =>
  within(list).getByRole("button", { name });

/** Opens the editor for one entity and character, once its fields have loaded. */
async function edit(routes: Routes, name: string) {
  const opened = openApp(routes, path);
  await opened.user.click(cell(await matrix(), name));
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findByRole("combobox", { name: "Knowledge scope" });
  return { ...opened, dialog };
}

describe("Grants", () => {
  it("is refused to a player without reading any grants", async () => {
    const { backend } = openApp(
      {
        ...dm,
        "GET /lobby": lobby({
          campaigns: [{ ...campaigns.owned, role: "player", is_owner: false }],
        }),
      },
      path,
    );
    expect(
      await screen.findByRole("heading", {
        name: "The grants table is for the DM.",
      }),
    ).toBeTruthy();
    expect(backend.callsTo("GET /campaigns/{campaign_id}/grants")).toHaveLength(
      0,
    );
    expect(backend.callsTo(LIST)).toHaveLength(0);
  });

  it("states every character's scope in words", async () => {
    openApp(dm, path);
    const list = await matrix();
    expect(
      screen.getByRole("heading", { name: "Knowledge at your table" }),
    ).toBeTruthy();
    for (const name of [
      "Edit Orla Fenn for Brannoch Stone: Partial",
      "Edit Orla Fenn for Wren Ashby: Not granted",
      "Edit The Old Marsh Road for Brannoch Stone: Full by default",
      "Edit Cellar key for Wren Ashby: Name only",
      "Edit Cellar key for Brannoch Stone: Not granted",
    ])
      expect(cell(list, name)).toBeTruthy();
    expect(
      cell(list, "Edit Cellar key for Wren Ashby: Name only").textContent,
    ).toBe("Wren Ashby: Name only");
    expect(
      screen
        .getByRole("link", { name: "Back to the table" })
        .getAttribute("href"),
    ).toBe(`/campaigns/${ids.owned}/session`);
  });

  it("reads every page of entities", async () => {
    const { backend } = openApp(
      {
        ...dm,
        [LIST]: (call) =>
          call.query.get("cursor")
            ? { items: [dmEntities[2]!], total: 2, next_cursor: null }
            : { items: [dmEntities[0]!], total: 2, next_cursor: "500" },
      },
      path,
    );
    const list = await matrix();
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(
      backend.callsTo(LIST).map((call) => call.query.get("limit")),
    ).toEqual(["500", "500"]);
    expect(backend.callsTo(LIST)[1]?.query.get("cursor")).toBe("500");
  });

  it("filters by name, type and the session a grant was made in", async () => {
    const { user } = openApp(dm, path);
    const list = await matrix();
    const names = () =>
      within(screen.getByRole("list", { name: "Knowledge grants" }))
        .getAllByRole("listitem")
        .map((row) => row.textContent);
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);

    await user.type(screen.getByLabelText("Find knowledge"), "marsh");
    expect(names()).toHaveLength(1);
    expect(names()[0]).toContain("The Old Marsh Road");
    await user.clear(screen.getByLabelText("Find knowledge"));

    await chooseOption(document.body, "Entity type", "item");
    expect(names()).toHaveLength(1);
    expect(names()[0]).toContain("Cellar key");
    await chooseOption(document.body, "Entity type", "All types");

    await chooseOption(
      document.body,
      "Granted in session",
      `${new Date(activeSession.started_at).toLocaleDateString()} · active`,
    );
    expect(names()).toHaveLength(1);
    expect(names()[0]).toContain("Orla Fenn");
  });

  it("opens the reveal drawer with the campaign's characters", async () => {
    const { user } = openApp(dm, path);
    await matrix();
    await user.click(screen.getByRole("button", { name: "Reveal knowledge" }));
    const dialog = await screen.findByRole("dialog");
    await chooseOption(
      dialog,
      "Hide knowledge already granted to",
      "Wren Ashby",
    );
    expect(
      within(dialog).getByRole("combobox", {
        name: "Hide knowledge already granted to",
      }).textContent,
    ).toBe("Wren Ashby");
  });

  it("starts from the saved grant and previews before it can be changed", async () => {
    const { user, backend, dialog } = await edit(
      dm,
      "Edit Orla Fenn for Brannoch Stone: Partial",
    );
    expect(
      within(dialog).getByRole("heading", {
        name: "Orla Fenn · Brannoch Stone",
      }),
    ).toBeTruthy();
    expect(
      within(dialog).getByRole<HTMLInputElement>("checkbox", {
        name: "disposition",
      }).checked,
    ).toBe(true);
    expect(
      within(dialog).getByRole<HTMLInputElement>("checkbox", {
        name: "occupation",
      }).checked,
    ).toBe(false);
    expect(
      within(dialog).getByLabelText<HTMLTextAreaElement>("Detail to share")
        .value,
    ).toBe("Counts the silver twice.");
    expect(
      within(dialog).queryByRole("button", { name: "Confirm knowledge" }),
    ).toBeNull();

    await chooseOption(dialog, "Knowledge scope", "Full knowledge");
    await user.click(
      within(dialog).getByRole("button", { name: "Preview knowledge" }),
    );
    expect(
      await within(dialog).findByRole("heading", {
        name: "What Brannoch Stone will see",
      }),
    ).toBeTruthy();
    expect(
      within(dialog)
        .getAllByRole("term")
        .map((term) => term.textContent),
    ).toEqual(["occupation", "disposition", "description"]);
    expect(backend.callsTo(PREVIEW)[0]?.body).toEqual({
      grants: [
        {
          entity_id: entityIds.keeper,
          player_character_id: brannoch.id,
          grant_scope: "full",
          revealed_details: null,
        },
      ],
    });

    await user.click(
      within(dialog).getByRole("button", { name: "Confirm knowledge" }),
    );
    expect(await findToast("Knowledge updated.")).toBeTruthy();
    const [call] = backend.callsTo(PATCH);
    expect(call?.params.grant_id).toBe(keeperGrant.id);
    // Leaving partial scope clears the saved details.
    expect(call?.body).toEqual({ grant_scope: "full", revealed_details: {} });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(
      backend.callsTo("GET /campaigns/{campaign_id}/grants").length,
    ).toBeGreaterThan(1);
  });

  it("saves the picked fields and clue of a partial grant", async () => {
    const { user, backend, dialog } = await edit(
      dm,
      "Edit Orla Fenn for Brannoch Stone: Partial",
    );
    await user.click(
      within(dialog).getByRole("checkbox", { name: "occupation" }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Preview knowledge" }),
    );
    await user.click(
      await within(dialog).findByRole("button", { name: "Confirm knowledge" }),
    );
    await waitFor(() => expect(backend.callsTo(PATCH)).toHaveLength(1));
    expect(backend.callsTo(PATCH)[0]?.body).toEqual({
      grant_scope: "partial",
      revealed_details: {
        occupation: "Keeper of the Gilded Eel",
        disposition: "wary",
        clue: "Counts the silver twice.",
      },
    });
  });

  it("creates a grant for a character who has none", async () => {
    const { user, backend, dialog } = await edit(
      dm,
      "Edit Cellar key for Brannoch Stone: Not granted",
    );
    expect(
      within(dialog).queryByRole("button", { name: "Retract knowledge" }),
    ).toBeNull();
    await user.click(
      within(dialog).getByRole("button", { name: "Preview knowledge" }),
    );
    expect(
      await within(dialog).findByText("You recognize this name."),
    ).toBeTruthy();
    expect(within(dialog).queryAllByRole("term")).toHaveLength(0);
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm knowledge" }),
    );
    expect(await findToast("Knowledge shared.")).toBeTruthy();
    expect(backend.callsTo(BULK)[0]?.body).toEqual({
      grants: [
        {
          entity_id: entityIds.key,
          player_character_id: brannoch.id,
          grant_scope: "name_only",
          revealed_details: null,
        },
      ],
    });
    expect(backend.callsTo(PATCH)).toHaveLength(0);
  });

  it("starts an ungranted global entity at its default, full", async () => {
    const { dialog } = await edit(
      dm,
      "Edit The Old Marsh Road for Wren Ashby: Full by default",
    );
    expect(
      within(dialog).getByRole("combobox", { name: "Knowledge scope" })
        .textContent,
    ).toBe("Full knowledge");
  });

  it.each([
    [false, "false"],
    [true, "true"],
  ])("retracts a grant, silently: %s", async (silent, sent) => {
    const { user, backend, dialog } = await edit(
      dm,
      "Edit Orla Fenn for Brannoch Stone: Partial",
    );
    if (silent)
      await user.click(
        within(dialog).getByRole("checkbox", { name: "Retract silently" }),
      );
    await user.click(
      within(dialog).getByRole("button", { name: "Retract knowledge" }),
    );
    expect(await findToast("Knowledge retracted.")).toBeTruthy();
    const [call] = backend.callsTo(REVOKE);
    expect(call?.params.grant_id).toBe(keeperGrant.id);
    expect(call?.query.get("silent")).toBe(sent);
  });
});
