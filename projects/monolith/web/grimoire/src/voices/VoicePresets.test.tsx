import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Routes } from "../test/backend";
import { chooseOption, findToast } from "../test/app";
import { ids } from "../test/fixtures";
import {
  npcIds,
  npcMatches,
  turnsRoutes,
  voices,
} from "../test/fixtures/turns";
import { renderPanel } from "../turns/renderPanel";
import type { VoicePreset } from "./voices";
import { VoicePresets } from "./VoicePresets";

const SAVE = "PUT /campaigns/{campaign_id}/voices/{speaker_key}";
const DELETE = "DELETE /campaigns/{campaign_id}/voices/{speaker_key}";
const SEARCH = "GET /campaigns/{campaign_id}/entities";

async function open(presets: VoicePreset[] = voices, routes: Routes = {}) {
  const view = renderPanel(
    <VoicePresets campaignId={ids.owned} presets={presets} />,
    {
      ...turnsRoutes,
      [SEARCH]: npcMatches as unknown as Record<string, unknown>,
      ...routes,
    },
  );
  await view.user.click(screen.getByRole("button", { name: /Voice presets/ }));
  await screen.findByRole("combobox", { name: "Preset speaker" });
  return {
    ...view,
    receive: (next: VoicePreset[]) =>
      view.rerender(<VoicePresets campaignId={ids.owned} presets={next} />),
  };
}

const text = (name: string) =>
  screen.getByRole("textbox", { name }) as HTMLInputElement;
const number = (name: string) =>
  screen.getByRole("spinbutton", { name }) as HTMLInputElement;
const button = (name: string) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
const held = (name: string) =>
  button(name).disabled ||
  button(name).getAttribute("aria-disabled") === "true";
const choose = (option: string) =>
  chooseOption(document.body, "Preset speaker", option);

describe("VoicePresets", () => {
  it("loads the narrator's saved voice first", async () => {
    await open();
    expect(text("Language hint").value).toBe("en-GB");
    expect(text("Preferred voice names").value).toBe("English, North");
    expect(number("Voice rate").value).toBe("0.9");
    expect(number("Voice pitch").value).toBe("1");
  });

  it("saves the hint, rate and pitch under the chosen speaker", async () => {
    const { user, backend } = await open();
    await user.clear(text("Language hint"));
    await user.type(text("Language hint"), " en-US ");
    await user.clear(text("Preferred voice names"));
    await user.type(text("Preferred voice names"), "Samantha, , Daniel ,");
    await user.click(button("Save voice"));
    await findToast("Voice saved.");
    const [call] = backend.callsTo(SAVE);
    expect(call!.params).toEqual({
      campaign_id: ids.owned,
      speaker_key: "narrator",
    });
    expect(call!.body).toEqual({
      voice_hint: { lang: "en-US", names: ["Samantha", "Daniel"] },
      rate: 0.9,
      pitch: 1,
    });
  });

  it("sends no language when the hint is blank", async () => {
    const { user, backend } = await open();
    await user.clear(text("Language hint"));
    await user.click(button("Save voice"));
    await findToast("Voice saved.");
    expect(
      (backend.callsTo(SAVE)[0]!.body as { voice_hint: object }).voice_hint,
    ).toEqual({ lang: null, names: ["English", "North"] });
  });

  it("loads a saved NPC voice by name and deletes it by its key", async () => {
    const { user, backend } = await open();
    await choose("The Pale Warden");
    expect(number("Voice pitch").value).toBe("0.4");
    await user.click(button("Delete voice"));
    await findToast("Voice deleted.");
    expect(backend.callsTo(DELETE)[0]!.params.speaker_key).toBe(npcIds.warden);
  });

  it("cannot delete a voice that was never saved", async () => {
    await open([]);
    expect(held("Delete voice")).toBe(true);
    expect(held("Save voice")).toBe(false);
    expect(number("Voice rate").value).toBe("1");
  });

  it("refuses more than eight preferred names", async () => {
    const { user } = await open();
    await user.clear(text("Preferred voice names"));
    await user.type(text("Preferred voice names"), "a,b,c,d,e,f,g,h,i");
    expect(held("Save voice")).toBe(true);
    expect(
      screen.getAllByText("Use at most eight names of 64 characters.")[0],
    ).toBeTruthy();
  });

  it("saves under a free label, with its spaces encoded in the path", async () => {
    const { user, backend } = await open();
    await choose("Free label");
    expect(held("Save voice")).toBe(true);
    await user.type(text("Preset label"), " Old Tam ");
    await user.click(button("Save voice"));
    await findToast("Voice saved.");
    expect(backend.callsTo(SAVE)[0]!.params.speaker_key).toBe("Old Tam");
  });

  it.each(["..", "a/b", "ref:abc"])(
    "refuses the free label %s",
    async (label) => {
      const { user } = await open();
      await choose("Free label");
      await user.type(text("Preset label"), label);
      expect(held("Save voice")).toBe(true);
      expect(held("Delete voice")).toBe(true);
      expect(
        screen.getAllByText("Choose a narrator, NPC or speaker label.")[0],
      ).toBeTruthy();
    },
  );

  it("loads a saved free label when its name is typed", async () => {
    const { user } = await open();
    await choose("Free label");
    await user.type(text("Preset label"), "Innkeeper");
    expect(number("Voice rate").value).toBe("1.2");
    expect(held("Delete voice")).toBe(false);
  });

  it("finds an NPC by name and keys the preset to its id", async () => {
    const { user, backend } = await open(voices, {
      [SEARCH]: {
        items: [{ ...npcMatches.items[0]!, id: npcIds.ostler.toUpperCase() }],
        total: 1,
      },
    });
    await choose("Find an NPC");
    expect(held("Find NPC")).toBe(true);
    await user.type(
      screen.getByRole("searchbox", { name: "NPC name" }),
      " odda ",
    );
    await user.click(button("Find NPC"));
    await user.click(
      await screen.findByRole("button", { name: "Use Odda the Ostler" }),
    );
    const [search] = backend.callsTo(SEARCH);
    expect(Object.fromEntries(search!.query)).toEqual({
      q: "odda",
      type: "npc",
      limit: "30",
    });
    expect(
      screen.getByRole("combobox", { name: "Preset speaker" }).textContent,
    ).toContain("Odda the Ostler");
    await user.click(button("Save voice"));
    await findToast("Voice saved.");
    // Saved entity keys are canonical lowercase ids.
    expect(backend.callsTo(SAVE)[0]!.params.speaker_key).toBe(npcIds.ostler);
  });

  it("tells same-named NPCs apart by their source", async () => {
    const { user } = await open(voices, {
      [SEARCH]: {
        items: [
          { id: npcIds.ostler, name: "Guard", source_book: "Keep on the Fen" },
          { id: npcIds.gone, name: "guard", source_type: "homebrew" },
          { id: npcIds.warden, name: "Captain" },
        ],
        total: 40,
      },
    });
    await choose("Find an NPC");
    await user.type(screen.getByRole("searchbox", { name: "NPC name" }), "g");
    fireEvent.keyDown(screen.getByRole("searchbox", { name: "NPC name" }), {
      key: "Enter",
    });
    expect(
      await screen.findByRole("button", {
        name: "Use Guard (Keep on the Fen)",
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Use guard (Homebrew)" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Use Captain" })).toBeTruthy();
    expect(
      screen.getByText("Showing 3 of 40. Type more of the name."),
    ).toBeTruthy();
  });

  it("says when no NPC matches", async () => {
    const { user } = await open(voices, { [SEARCH]: { items: [], total: 0 } });
    await choose("Find an NPC");
    await user.type(screen.getByRole("searchbox", { name: "NPC name" }), "zz");
    await user.click(button("Find NPC"));
    expect(await screen.findByText("No NPC matches that name.")).toBeTruthy();
  });

  it("reports a failed NPC search beside the field", async () => {
    const { user } = await open(voices, {
      [SEARCH]: { status: 403, detail: "DM only" },
    });
    await choose("Find an NPC");
    await user.type(screen.getByRole("searchbox", { name: "NPC name" }), "zz");
    await user.click(button("Find NPC"));
    expect((await screen.findAllByText("DM only"))[0]).toBeTruthy();
  });

  it("offers only deletion for an NPC that left the campaign", async () => {
    const { user, backend } = await open([
      ...voices,
      { ...voices[1]!, speaker_key: npcIds.gone, speaker_name: null },
    ]);
    await choose("Unavailable NPC");
    expect(
      screen.getByText(
        "This NPC is no longer in the campaign. Delete its preset.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save voice" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Language hint" })).toBeNull();
    await user.click(button("Delete voice"));
    await findToast("Voice deleted.");
    expect(backend.callsTo(DELETE)[0]!.params.speaker_key).toBe(npcIds.gone);
  });

  it("falls back to the narrator when the chosen preset is deleted", async () => {
    const { receive } = await open();
    await choose("The Pale Warden");
    receive([voices[0]!]);
    await waitFor(() => expect(number("Voice rate").value).toBe("0.9"));
    expect(
      screen.getByRole("combobox", { name: "Preset speaker" }).textContent,
    ).toContain("Narrator");
  });

  it("keeps unsaved edits when a poll brings the same presets", async () => {
    const { user, receive } = await open();
    await user.clear(text("Language hint"));
    await user.type(text("Language hint"), "fr-FR");
    receive(voices.map((preset) => ({ ...preset })));
    expect(text("Language hint").value).toBe("fr-FR");
  });

  it("keeps the form and reports a refused save", async () => {
    const { user } = await open(voices, {
      [SAVE]: { status: 404, detail: "NPC not found in campaign" },
    });
    await user.click(button("Save voice"));
    await findToast("NPC not found in campaign");
    expect(text("Language hint").value).toBe("en-GB");
    expect(
      within(document.body).getByRole("button", { name: "Save voice" }),
    ).toBeTruthy();
  });
});
