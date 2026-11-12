/** Invented sheets data and the routes that serve it in tests and the preview. */
import type {
  CharacterSheetHistoryView,
  CharacterSheetV1,
  CharacterSheetView,
  CharacterView,
  SheetOptionsView,
} from "../../api/schema";
import type { SheetDerived } from "../../api/sheets";
import type { Call, Routes } from "../backend";
import { characters, ids } from "../fixtures";

export const versionIds = {
  miraApproved: "61111111-1111-4111-8111-111111111111",
  miraReturned: "62222222-2222-4222-8222-222222222222",
  brannochApproved: "63333333-3333-4333-8333-333333333333",
  brannochSubmitted: "64444444-4444-4444-8444-444444444444",
  fresh: "65555555-5555-4555-8555-555555555555",
};

/** The player's own character in the joined campaign. */
export const mira: CharacterView = {
  id: ids.mira,
  campaign_id: ids.joined,
  player_name: "Ash",
  character_name: "Mira Vale",
  class_name: "Wizard",
  level: 3,
  sheet: {},
};

/** A character in the campaign the viewer runs. */
export const brannoch: CharacterView = characters[0]!;

export const miraSheet: CharacterSheetV1 = {
  schema_version: 1,
  ancestry: "Elf",
  class_name: "Wizard",
  level: 3,
  ability_scores: {
    strength: 8,
    dexterity: 14,
    constitution: 12,
    intelligence: 16,
    wisdom: 13,
    charisma: 10,
  },
};

export const miraDerived: SheetDerived = {
  ability_modifiers: {
    strength: -1,
    dexterity: 2,
    constitution: 1,
    intelligence: 3,
    wisdom: 1,
    charisma: 0,
  },
  proficiency_bonus: 2,
  saving_throw_bonuses: {
    strength: -1,
    dexterity: 2,
    constitution: 1,
    intelligence: 5,
    wisdom: 3,
    charisma: 0,
  },
  attack_bonuses: { melee: 1, ranged: 4 },
  unarmored_armor_class: 12,
  max_hit_points: 17,
  hit_die: "d6",
  corpus: {
    ancestry_entity_id: "71111111-1111-4111-8111-111111111111",
    class_entity_id: "72222222-2222-4222-8222-222222222222",
  },
};

export const brannochSheet: CharacterSheetV1 = {
  schema_version: 1,
  ancestry: "Dwarf",
  class_name: "Fighter",
  level: 4,
  ability_scores: {
    strength: 16,
    dexterity: 12,
    constitution: 15,
    intelligence: 9,
    wisdom: 11,
    charisma: 8,
  },
};

export const brannochDerived: SheetDerived = {
  ability_modifiers: {
    strength: 3,
    dexterity: 1,
    constitution: 2,
    intelligence: -1,
    wisdom: 0,
    charisma: -1,
  },
  proficiency_bonus: 2,
  saving_throw_bonuses: {
    strength: 5,
    dexterity: 1,
    constitution: 4,
    intelligence: -1,
    wisdom: 0,
    charisma: -1,
  },
  attack_bonuses: { melee: 5, ranged: 3 },
  unarmored_armor_class: 11,
  max_hit_points: 36,
  hit_die: "d10",
  corpus: {
    ancestry_entity_id: "73333333-3333-4333-8333-333333333333",
    class_entity_id: "74444444-4444-4444-8444-444444444444",
  },
};

/** The corpus choices offered to a character, with the facts behind each. */
export const sheetOptions = (
  overrides: Partial<SheetOptionsView> = {},
): SheetOptionsView => ({
  ancestries: [
    {
      id: "73333333-3333-4333-8333-333333333333",
      name: "Dwarf",
      size: "Medium",
      speed: "25 feet",
    },
    {
      id: "71111111-1111-4111-8111-111111111111",
      name: "Elf",
      size: "Medium",
      speed: "30 feet",
    },
    {
      id: "75555555-5555-4555-8555-555555555555",
      name: "Marsh Otterkin",
      size: "Small",
      speed: null,
    },
  ],
  classes: [
    {
      id: "74444444-4444-4444-8444-444444444444",
      name: "Fighter",
      hit_die: "d10",
      primary_ability: "Strength or Dexterity",
      saves: "Strength, Constitution",
    },
    {
      id: "76666666-6666-4666-8666-666666666666",
      name: "Lamplighter",
      hit_die: "d8",
      primary_ability: null,
      saves: "Wisdom and Charisma",
    },
    {
      id: "72222222-2222-4222-8222-222222222222",
      name: "Wizard",
      hit_die: "d6",
      primary_ability: "Intelligence",
      saves: "Intelligence, Wisdom",
    },
  ],
  ...overrides,
});

/** One of Mira's versions. Pass the fields a case cares about. */
export const sheetVersion = (
  overrides: Partial<CharacterSheetView> = {},
): CharacterSheetView => ({
  id: versionIds.miraApproved,
  campaign_id: ids.joined,
  player_character_id: ids.mira,
  version: 1,
  contract_version: 1,
  status: "approved",
  sheet: { ...miraSheet },
  derived: { ...miraDerived },
  created_by_email: "ash@example.test",
  submitted_at: "2026-10-03T18:00:00Z",
  decided_at: "2026-10-04T18:00:00Z",
  decision_comment: null,
  decided_by_email: "wren@example.test",
  created_at: "2026-10-03T17:00:00Z",
  updated_at: "2026-10-04T18:00:00Z",
  ...overrides,
});

const undecided = {
  submitted_at: null,
  decided_at: null,
  decided_by_email: null,
};

export const miraReturned = sheetVersion({
  id: versionIds.miraReturned,
  version: 2,
  status: "returned",
  sheet: { ...miraSheet, level: 4 },
  decision_comment: "We have not reached level 4 yet.",
  created_at: "2026-11-01T17:00:00Z",
  submitted_at: "2026-11-01T17:30:00Z",
  decided_at: "2026-11-02T09:00:00Z",
  updated_at: "2026-11-02T09:00:00Z",
});

export const miraDraft = sheetVersion({
  id: versionIds.fresh,
  version: 3,
  status: "draft",
  created_at: "2026-11-05T17:00:00Z",
  updated_at: "2026-11-05T17:00:00Z",
  ...undecided,
});

export const brannochSubmitted = sheetVersion({
  id: versionIds.brannochSubmitted,
  campaign_id: ids.owned,
  player_character_id: ids.spare,
  version: 2,
  status: "submitted",
  sheet: { ...brannochSheet },
  derived: { ...brannochDerived },
  created_by_email: "rowan@example.test",
  created_at: "2026-11-08T17:00:00Z",
  submitted_at: "2026-11-08T17:30:00Z",
  updated_at: "2026-11-08T17:30:00Z",
  decided_at: null,
  decided_by_email: null,
});

const brannochApproved = sheetVersion({
  id: versionIds.brannochApproved,
  campaign_id: ids.owned,
  player_character_id: ids.spare,
  sheet: { ...brannochSheet, level: 3 },
  derived: { ...brannochDerived, max_hit_points: 28 },
  created_by_email: "rowan@example.test",
  decided_by_email: "ash@example.test",
});

/** The player's view of their own character, newest version first. */
export const playerHistory = (
  versions: CharacterSheetView[] = [miraReturned, sheetVersion()],
): CharacterSheetHistoryView => ({
  character: mira,
  viewer_role: "player",
  versions,
});

/** The DM's view of a character with a version awaiting their decision. */
export const dmHistory = (
  versions: CharacterSheetView[] = [brannochSubmitted, brannochApproved],
): CharacterSheetHistoryView => ({
  character: brannoch,
  viewer_role: "dm",
  versions,
});

/** A write's reply: the addressed version with the fields the write changes. */
const written =
  (changes: (call: Call) => Partial<CharacterSheetView>) =>
  (call: Call): CharacterSheetView =>
    sheetVersion({
      id: call.params.sheet_version_id ?? versionIds.fresh,
      campaign_id: call.params.campaign_id!,
      player_character_id: call.params.player_character_id!,
      ...undecided,
      ...changes(call),
    });

const draftFrom = (call: Call): Partial<CharacterSheetView> => ({
  status: "draft",
  sheet: call.body as Record<string, unknown>,
});

const decided =
  (status: "approved" | "returned") =>
  (call: Call): Partial<CharacterSheetView> => ({
    status,
    decision_comment: (call.body as { comment: string | null }).comment,
    decided_by_email: "ash@example.test",
    decided_at: "2026-11-11T09:00:00Z",
  });

export const sheetsRoutes: Routes = {
  // Each campaign lists its own characters, so the preview matches the lobby.
  "GET /campaigns/{campaign_id}/characters": (call) =>
    call.params.campaign_id === ids.owned
      ? characters
      : call.params.campaign_id === ids.joined
        ? [mira]
        : [],
  "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets": (
    call,
  ) =>
    call.params.player_character_id === ids.mira
      ? playerHistory()
      : dmHistory(),
  "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets/options":
    sheetOptions(),
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/drafts":
    written(draftFrom),
  "PATCH /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}":
    written(draftFrom),
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/submit":
    written(() => ({
      status: "submitted",
      submitted_at: "2026-11-11T09:00:00Z",
    })),
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/approve":
    written(decided("approved")),
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/return":
    written(decided("returned")),
};
