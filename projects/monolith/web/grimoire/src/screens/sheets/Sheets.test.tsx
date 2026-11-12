import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Routes } from "../../test/backend";
import { chooseOption, findToast, openApp } from "../../test/app";
import { campaigns, ids, lobby } from "../../test/fixtures";
import {
  brannochSheet,
  brannochSubmitted,
  dmHistory,
  miraDraft,
  miraSheet,
  playerHistory,
  sheetOptions,
  sheetsRoutes,
  sheetVersion,
  versionIds,
} from "../../test/fixtures/sheets";

const SHEETS =
  "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets";
const CREATE =
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/drafts";
const UPDATE =
  "PATCH /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}";
const OPTIONS =
  "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets/options";
const SUBMIT =
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/submit";
const APPROVE =
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/approve";
const RETURN =
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/return";

const playerRoutes = (routes: Routes = {}): Routes => ({
  ...sheetsRoutes,
  "GET /lobby": lobby({ campaigns: [campaigns.joined] }),
  ...routes,
});

const dmRoutes = (routes: Routes = {}): Routes => ({
  ...sheetsRoutes,
  "GET /lobby": lobby({ campaigns: [campaigns.owned] }),
  ...routes,
});

async function open(routes: Routes, name: string, path = "/sheets") {
  const app = openApp(routes, path);
  const sheet = await screen.findByRole("region", { name });
  await within(sheet).findByText(/^Version history/);
  // The editor loads its corpus choices after the history.
  await waitFor(() =>
    expect(sheet.querySelector('[aria-busy="true"]')).toBeNull(),
  );
  return { ...app, sheet };
}

const chosen = (sheet: HTMLElement, control: string) =>
  within(sheet).getByRole("combobox", { name: control }).textContent;

/** The modifier shown with one ability score. */
const modifier = (sheet: HTMLElement, label: string) =>
  (within(sheet).getByLabelText(label).getAttribute("aria-describedby") ?? "")
    .split(" ")
    .map((id) => document.getElementById(id)?.textContent)
    .find((text) => text?.startsWith("Modifier"));

/** The value shown for one line of the preview. */
const calculated = (sheet: HTMLElement, label: string) =>
  within(sheet).getByText(label).closest("dt")?.nextElementSibling?.textContent;

const withDraft = (routes: Routes = {}) =>
  playerRoutes({
    [SHEETS]: playerHistory([miraDraft, sheetVersion()]),
    ...routes,
  });

// The test DOM has no layout, so it has no scrollIntoView to restore.
afterEach(() => {
  Reflect.deleteProperty(Element.prototype, "scrollIntoView");
});

describe("Character sheets", () => {
  it("names the page and links back to the lobby", async () => {
    await open(playerRoutes(), "Mira Vale");
    expect(
      screen.getByRole("heading", { level: 1, name: "Character sheets" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Your campaigns" }).getAttribute("href"),
    ).toBe("/");
  });

  it("says so when the viewer has no campaigns", async () => {
    openApp({ "GET /lobby": lobby({ campaigns: [] }) }, "/sheets");
    expect(await screen.findByText("No campaigns yet")).toBeTruthy();
    expect(
      screen.getByText(
        "A campaign DM must add your verified account before sheets appear here.",
      ),
    ).toBeTruthy();
  });

  it("says so when a campaign has no assigned characters", async () => {
    openApp(
      {
        ...sheetsRoutes,
        "GET /lobby": lobby({ campaigns: [campaigns.seatless] }),
      },
      "/sheets",
    );
    expect(
      await screen.findByRole("heading", { level: 2, name: "The Long Road" }),
    ).toBeTruthy();
    expect(await screen.findByText("No assigned characters.")).toBeTruthy();
  });

  it("offers a retry when sheets cannot load", async () => {
    openApp(
      { "GET /lobby": { status: 500, detail: "Database unavailable." } },
      "/sheets",
    );
    expect(
      await screen.findByText("Could not load character sheets"),
    ).toBeTruthy();
    expect(screen.getByText("Database unavailable.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });

  it("scrolls to the character the lobby linked to once it has loaded", async () => {
    const scrolled: string[] = [];
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this.id);
    };
    await open(playerRoutes(), "Mira Vale", `/sheets#character-${ids.mira}`);
    await waitFor(() => expect(scrolled).toEqual([`character-${ids.mira}`]));
  });
});

describe("Character sheets, as the player", () => {
  it("opens the editor on the latest version with its mechanics and ruling", async () => {
    const { sheet } = await open(playerRoutes(), "Mira Vale");
    expect(within(sheet).getByText("Wizard · level 3")).toBeTruthy();
    expect(within(sheet).getByText("Returned")).toBeTruthy();
    expect(chosen(sheet, "Ancestry")).toContain("Elf");
    expect(chosen(sheet, "Class")).toContain("Wizard");
    expect(
      (within(sheet).getByLabelText("Level") as HTMLInputElement).value,
    ).toBe("4");
    expect(
      (within(sheet).getByLabelText("INT") as HTMLInputElement).value,
    ).toBe("16");
    expect(modifier(sheet, "INT")).toBe("Modifier +3");
    expect(modifier(sheet, "STR")).toBe("Modifier -1");
    expect(calculated(sheet, "INT save")).toContain("+5, proficient");
    expect(calculated(sheet, "Hit die")).toContain("d6");
    // The saved version's own summary gives way to the live preview.
    expect(within(sheet).queryByText("Calculated mechanics")).toBeNull();
    expect(
      within(sheet).getByText("We have not reached level 4 yet."),
    ).toBeTruthy();
    expect(within(sheet).getByText(/wren@example\.test/)).toBeTruthy();
    expect(
      within(sheet).queryByRole("button", { name: "Approve version" }),
    ).toBeNull();
    expect(
      within(sheet).queryByRole("button", { name: "Submit to DM" }),
    ).toBeNull();
  });

  it("starts blank at level 1 with tens when there is no sheet yet", async () => {
    const { sheet } = await open(
      playerRoutes({ [SHEETS]: playerHistory([]) }),
      "Mira Vale",
    );
    expect(within(sheet).getByText("No sheet")).toBeTruthy();
    expect(chosen(sheet, "Class")).toContain("Choose a class");
    expect(chosen(sheet, "Ancestry")).toContain("Choose an ancestry");
    expect(
      within(sheet).getByText(
        "Choose a class to see hit points and saving throws.",
      ),
    ).toBeTruthy();
    expect(within(sheet).queryByText("Maximum hit points")).toBeNull();
    expect(
      (within(sheet).getByLabelText("Level") as HTMLInputElement).value,
    ).toBe("1");
    expect(
      (within(sheet).getByLabelText("STR") as HTMLInputElement).value,
    ).toBe("10");
    expect(within(sheet).queryByText("Calculated mechanics")).toBeNull();
  });

  it("starts the next version from the edited facts", async () => {
    const { user, sheet, backend } = await open(playerRoutes(), "Mira Vale");
    await chooseOption(sheet, "Ancestry", "Marsh Otterkin");
    const level = within(sheet).getByLabelText("Level");
    await user.clear(level);
    await user.type(level, "3");
    await user.click(
      within(sheet).getByRole("button", { name: "Start new draft" }),
    );
    expect(await findToast("Draft saved.")).toBeTruthy();
    const [call] = backend.callsTo(CREATE);
    expect(call?.params).toEqual({
      campaign_id: ids.joined,
      player_character_id: ids.mira,
    });
    expect(call?.body).toEqual({ ...miraSheet, ancestry: "Marsh Otterkin" });
    expect(backend.callsTo(SHEETS).length).toBeGreaterThan(1);
  });

  it("saves changes to the open draft", async () => {
    const { user, sheet, backend } = await open(withDraft(), "Mira Vale");
    const wisdom = within(sheet).getByLabelText("WIS");
    await user.clear(wisdom);
    await user.type(wisdom, "15");
    await user.click(within(sheet).getByRole("button", { name: "Save draft" }));
    expect(await findToast("Draft saved.")).toBeTruthy();
    const [call] = backend.callsTo(UPDATE);
    expect(call?.params.sheet_version_id).toBe(versionIds.fresh);
    expect(call?.body).toEqual({
      ...miraSheet,
      ability_scores: { ...miraSheet.ability_scores, wisdom: 15 },
    });
    expect(backend.callsTo(CREATE)).toHaveLength(0);
  });

  it("keeps scores and level inside the sheet contract's bounds", async () => {
    const { user, sheet, backend } = await open(withDraft(), "Mira Vale");
    const set = async (label: string, value: string) => {
      const field = within(sheet).getByLabelText(label);
      await user.clear(field);
      await user.type(field, value);
    };
    expect(within(sheet).getByText("Whole numbers from 3 to 20.")).toBeTruthy();
    await set("STR", "25");
    await set("DEX", "2");
    await set("CON", "12.5");
    await set("Level", "21");
    await user.click(within(sheet).getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(backend.callsTo(UPDATE)).toHaveLength(1));
    expect(backend.callsTo(UPDATE)[0]?.body).toEqual({
      ...miraSheet,
      level: 20,
      ability_scores: {
        ...miraSheet.ability_scores,
        strength: 20,
        dexterity: 3,
      },
    });
  });

  it("offers the ancestries and classes the corpus holds for the character", async () => {
    const { user, sheet, backend } = await open(
      playerRoutes({ [SHEETS]: playerHistory([]) }),
      "Mira Vale",
    );
    expect(backend.callsTo(OPTIONS)[0]?.params).toEqual({
      campaign_id: ids.joined,
      player_character_id: ids.mira,
    });
    const options = (control: string) => {
      const combobox = within(sheet).getByRole("combobox", { name: control });
      const listbox = document.getElementById(
        combobox.getAttribute("aria-controls") ?? "",
      )!;
      return within(listbox)
        .getAllByRole("option")
        .map((option) => option.textContent);
    };
    await user.click(within(sheet).getByRole("combobox", { name: "Class" }));
    expect(options("Class")).toEqual(["Fighter", "Lamplighter", "Wizard"]);
    await user.keyboard("{Escape}");
    await user.click(within(sheet).getByRole("combobox", { name: "Ancestry" }));
    expect(options("Ancestry")).toEqual(["Dwarf", "Elf", "Marsh Otterkin"]);
    // Nothing is typed: the names come from the corpus.
    expect(within(sheet).queryByRole("textbox", { name: /Class/ })).toBeNull();
  });

  it("shows a chosen class's facts and what Grimoire will calculate", async () => {
    const { user, sheet } = await open(
      playerRoutes({ [SHEETS]: playerHistory([]) }),
      "Mira Vale",
    );
    expect(
      within(sheet).getByRole("heading", {
        name: "What Grimoire will calculate",
      }),
    ).toBeTruthy();
    await chooseOption(sheet, "Class", "Fighter");
    expect(
      await within(sheet).findByText(
        "Hit die d10. Saving throws: Strength, Constitution. Primary ability: Strength or Dexterity.",
      ),
    ).toBeTruthy();
    await chooseOption(sheet, "Ancestry", "Dwarf");
    expect(
      await within(sheet).findByText("Size Medium. Speed 25 feet."),
    ).toBeTruthy();
    const set = async (label: string, value: string) => {
      const field = within(sheet).getByLabelText(label);
      await user.clear(field);
      await user.type(field, value);
      await user.tab();
    };
    await set("Level", "5");
    await set("STR", "16");
    await set("DEX", "14");
    await set("CON", "14");
    await set("WIS", "12");
    await set("CHA", "8");
    // The level 5 fighter grimoire/sheets_test.py pins.
    expect(modifier(sheet, "STR")).toBe("Modifier +3");
    expect(modifier(sheet, "CHA")).toBe("Modifier -1");
    expect(calculated(sheet, "Proficiency bonus")).toContain("+3");
    expect(calculated(sheet, "Armour class, unarmoured")).toContain("12");
    expect(calculated(sheet, "Maximum hit points")).toContain("44");
    expect(calculated(sheet, "Hit die")).toContain("d10");
    expect(calculated(sheet, "STR save")).toContain("+6, proficient");
    expect(calculated(sheet, "CON save")).toContain("+5, proficient");
    expect(calculated(sheet, "WIS save")).toContain("+1");
    expect(calculated(sheet, "WIS save")).not.toContain("proficient");
  });

  it("gives the server's refusal for a class entry it cannot calculate from", async () => {
    const { sheet, user, backend } = await open(
      withDraft({
        [OPTIONS]: sheetOptions({
          classes: [
            {
              id: "72222222-2222-4222-8222-222222222222",
              name: "Wizard",
              hit_die: "1d6",
              primary_ability: null,
              saves: "Intelligence, Wisdom",
            },
          ],
        }),
      }),
      "Mira Vale",
    );
    expect(
      within(sheet).getByText(
        "Grimoire cannot calculate hit points or saving throws for Wizard: class corpus entry has no supported hit die. Choose another class or ask your DM to fix the entry.",
      ),
    ).toBeTruthy();
    expect(within(sheet).queryByText("Maximum hit points")).toBeNull();
    // The choice is still the server's to refuse.
    await user.click(within(sheet).getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(backend.callsTo(UPDATE)).toHaveLength(1));
  });

  it("asks for a missing ancestry or class instead of saving", async () => {
    const { user, sheet, backend } = await open(
      playerRoutes({ [SHEETS]: playerHistory([]) }),
      "Mira Vale",
    );
    await user.click(
      within(sheet).getByRole("button", { name: "Start new draft" }),
    );
    expect(await within(sheet).findByText("Choose an ancestry.")).toBeTruthy();
    expect(within(sheet).getByText("Choose a class.")).toBeTruthy();
    expect(backend.callsTo(CREATE)).toHaveLength(0);
  });

  it("directs the player to the DM when the corpus offers no class", async () => {
    const { user, sheet, backend } = await open(
      playerRoutes({
        [SHEETS]: playerHistory([]),
        [OPTIONS]: sheetOptions({ classes: [] }),
      }),
      "Mira Vale",
    );
    expect(
      within(sheet).getByText(
        "No class is available to Mira Vale. Ask your DM to share a class with Mira Vale.",
      ),
    ).toBeTruthy();
    expect(within(sheet).queryByText(/No ancestry is available/)).toBeNull();
    const save = within(sheet).getByRole("button", { name: "Start new draft" });
    expect(
      save.hasAttribute("disabled") ||
        save.getAttribute("aria-disabled") === "true",
    ).toBe(true);
    await user.click(save);
    expect(backend.callsTo(CREATE)).toHaveLength(0);
  });

  it("directs the player to the DM when the corpus offers no ancestry", async () => {
    const { sheet } = await open(
      playerRoutes({
        [SHEETS]: playerHistory([]),
        [OPTIONS]: sheetOptions({ ancestries: [] }),
      }),
      "Mira Vale",
    );
    expect(
      within(sheet).getByText(
        "No ancestry is available to Mira Vale. Ask your DM to share an ancestry with Mira Vale.",
      ),
    ).toBeTruthy();
  });

  it("flags a saved choice the character can no longer use", async () => {
    const { user, sheet, backend } = await open(
      withDraft({
        [OPTIONS]: sheetOptions({
          classes: sheetOptions().classes.filter(
            ({ name }) => name !== "Wizard",
          ),
        }),
      }),
      "Mira Vale",
    );
    expect(
      within(sheet).getByText(
        "Wizard is no longer available to Mira Vale. Choose another class.",
      ),
    ).toBeTruthy();
    expect(chosen(sheet, "Class")).toContain("Choose a class");
    expect(chosen(sheet, "Ancestry")).toContain("Elf");
    await user.click(within(sheet).getByRole("button", { name: "Save draft" }));
    await user.click(
      within(sheet).getByRole("button", { name: "Submit to DM" }),
    );
    expect(backend.callsTo(UPDATE)).toHaveLength(0);
    expect(backend.callsTo(SUBMIT)).toHaveLength(0);

    await chooseOption(sheet, "Class", "Lamplighter");
    await user.click(within(sheet).getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(backend.callsTo(UPDATE)).toHaveLength(1));
    expect(backend.callsTo(UPDATE)[0]?.body).toEqual({
      ...miraSheet,
      class_name: "Lamplighter",
    });
  });

  it("keeps a saved choice selected whatever its letter case", async () => {
    const { sheet } = await open(
      withDraft({
        [SHEETS]: playerHistory([
          { ...miraDraft, sheet: { ...miraSheet, class_name: "wizard" } },
        ]),
      }),
      "Mira Vale",
    );
    expect(chosen(sheet, "Class")).toContain("Wizard");
    expect(
      within(sheet).getByText("Submitting locks version 3 for DM review."),
    ).toBeTruthy();
  });

  it("offers a retry when the choices cannot load", async () => {
    const { sheet } = await open(
      playerRoutes({
        [OPTIONS]: { status: 500, detail: "Database unavailable." },
      }),
      "Mira Vale",
    );
    expect(
      await within(sheet).findByText(
        "Could not load the choices for this sheet",
      ),
    ).toBeTruthy();
    expect(
      within(sheet).getByRole("button", { name: "Try again" }),
    ).toBeTruthy();
    expect(within(sheet).queryByRole("button", { name: /draft/ })).toBeNull();
  });

  it("shows why the backend refused a draft", async () => {
    const { user, sheet } = await open(
      withDraft({
        [UPDATE]: {
          status: 422,
          detail: "ancestry is not available in this campaign corpus",
        },
      }),
      "Mira Vale",
    );
    await user.click(within(sheet).getByRole("button", { name: "Save draft" }));
    expect(
      await findToast("ancestry is not available in this campaign corpus"),
    ).toBeTruthy();
  });

  it("submits the saved draft to the DM", async () => {
    const { user, sheet, backend } = await open(withDraft(), "Mira Vale");
    expect(
      within(sheet).getByText("Submitting locks version 3 for DM review."),
    ).toBeTruthy();
    await user.click(
      within(sheet).getByRole("button", { name: "Submit to DM" }),
    );
    expect(await findToast("Submitted to the DM.")).toBeTruthy();
    const [call] = backend.callsTo(SUBMIT);
    expect(call?.params).toEqual({
      campaign_id: ids.joined,
      player_character_id: ids.mira,
      sheet_version_id: versionIds.fresh,
    });
    expect(call?.body).toBeUndefined();
  });

  it("holds submission until edits are saved", async () => {
    const { user, sheet, backend } = await open(withDraft(), "Mira Vale");
    await chooseOption(sheet, "Class", "Fighter");
    expect(
      within(sheet).getByText("Save your changes before submitting version 3."),
    ).toBeTruthy();
    await user.click(
      within(sheet).getByRole("button", { name: "Submit to DM" }),
    );
    expect(backend.callsTo(SUBMIT)).toHaveLength(0);
  });

  it("locks a submitted version while it waits for the DM", async () => {
    const { sheet } = await open(
      playerRoutes({
        [SHEETS]: playerHistory([
          { ...miraDraft, status: "submitted" },
          sheetVersion(),
        ]),
      }),
      "Mira Vale",
    );
    expect(
      within(sheet).getByText("Version 3 is waiting for the DM."),
    ).toBeTruthy();
    expect(
      within(sheet).queryByRole("combobox", { name: "Ancestry" }),
    ).toBeNull();
    expect(within(sheet).getByText("Calculated mechanics")).toBeTruthy();
    expect(within(sheet).queryByRole("button", { name: /draft/ })).toBeNull();
    expect(
      within(sheet).queryByRole("button", { name: "Approve version" }),
    ).toBeNull();
  });

  it("lists every version, newest first", async () => {
    const { user, sheet } = await open(playerRoutes(), "Mira Vale");
    await user.click(within(sheet).getByText("Version history (2)"));
    const rows = await within(sheet).findAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("v2 · Returned"),
      expect.stringContaining("v1 · Approved"),
    ]);
  });

  it("says when no version exists yet", async () => {
    const { user, sheet } = await open(
      playerRoutes({ [SHEETS]: playerHistory([]) }),
      "Mira Vale",
    );
    await user.click(within(sheet).getByText("Version history (0)"));
    expect(
      await within(sheet).findByText("No versions submitted."),
    ).toBeTruthy();
  });
});

describe("Character sheets, as the DM", () => {
  it("shows the submitted facts and mechanics without an editor", async () => {
    const { sheet } = await open(dmRoutes(), "Brannoch Stone");
    expect(within(sheet).getByText("Submitted")).toBeTruthy();
    expect(
      within(sheet).getByRole("heading", { name: "Version 2" }),
    ).toBeTruthy();
    expect(within(sheet).getByText("Dwarf")).toBeTruthy();
    expect(within(sheet).getByText(String(brannochSheet.level))).toBeTruthy();
    expect(within(sheet).getByText("+3 / +5")).toBeTruthy();
    expect(
      within(sheet).queryByRole("combobox", { name: "Ancestry" }),
    ).toBeNull();
    expect(within(sheet).queryByRole("button", { name: /draft/ })).toBeNull();
  });

  it("approves a submitted version, with no comment needed", async () => {
    const { user, sheet, backend } = await open(dmRoutes(), "Brannoch Stone");
    await user.click(
      within(sheet).getByRole("button", { name: "Approve version" }),
    );
    expect(await findToast("Sheet approved.")).toBeTruthy();
    const [call] = backend.callsTo(APPROVE);
    expect(call?.params).toEqual({
      campaign_id: ids.owned,
      player_character_id: ids.spare,
      sheet_version_id: versionIds.brannochSubmitted,
    });
    expect(call?.body).toEqual({ comment: null });
    expect(backend.callsTo(SHEETS).length).toBeGreaterThan(1);
  });

  it("requires a comment to return a version", async () => {
    const { user, sheet, backend } = await open(dmRoutes(), "Brannoch Stone");
    await user.type(within(sheet).getByLabelText(/^Decision comment/), "   ");
    await user.click(
      within(sheet).getByRole("button", { name: "Return to player" }),
    );
    expect(
      await within(sheet).findByText("Say what the player should change."),
    ).toBeTruthy();
    expect(backend.callsTo(RETURN)).toHaveLength(0);
  });

  it("returns a version with the DM's comment", async () => {
    const { user, sheet, backend } = await open(dmRoutes(), "Brannoch Stone");
    await user.type(
      within(sheet).getByLabelText(/^Decision comment/),
      " Level 4 comes  after the vault. ",
    );
    await user.click(
      within(sheet).getByRole("button", { name: "Return to player" }),
    );
    expect(await findToast("Sheet returned to the player.")).toBeTruthy();
    const [call] = backend.callsTo(RETURN);
    expect(call?.params.sheet_version_id).toBe(versionIds.brannochSubmitted);
    expect(call?.body).toEqual({ comment: "Level 4 comes after the vault." });
  });

  it("shows the refusal when a decision is rejected", async () => {
    const { user, sheet } = await open(
      dmRoutes({
        [APPROVE]: {
          status: 409,
          detail: "only a submitted sheet can be decided",
        },
      }),
      "Brannoch Stone",
    );
    await user.click(
      within(sheet).getByRole("button", { name: "Approve version" }),
    );
    expect(
      await findToast("only a submitted sheet can be decided"),
    ).toBeTruthy();
  });

  it("offers no decision once a version is decided", async () => {
    const { sheet } = await open(
      dmRoutes({
        [SHEETS]: dmHistory([{ ...brannochSubmitted, status: "approved" }]),
      }),
      "Brannoch Stone",
    );
    expect(within(sheet).getByText("Approved")).toBeTruthy();
    expect(
      within(sheet).queryByRole("button", { name: "Approve version" }),
    ).toBeNull();
    expect(within(sheet).queryByLabelText(/^Decision comment/)).toBeNull();
  });
});
