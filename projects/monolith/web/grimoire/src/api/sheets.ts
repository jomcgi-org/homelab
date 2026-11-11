/** Character sheet reads and writes: drafts, submission and the DM's decision. */
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import { keys as lobbyKeys } from "./lobby";
import type {
  AbilityScores,
  CharacterSheetHistoryView,
  CharacterSheetV1,
  CharacterSheetView,
} from "./schema";

export type Ability = keyof AbilityScores;

export const ABILITIES: ReadonlyArray<{ name: Ability; label: string }> = [
  { name: "strength", label: "STR" },
  { name: "dexterity", label: "DEX" },
  { name: "constitution", label: "CON" },
  { name: "intelligence", label: "INT" },
  { name: "wisdom", label: "WIS" },
  { name: "charisma", label: "CHA" },
];

/** The bounds `CharacterSheetV1` enforces in `grimoire/sheets.py`. */
export const BOUNDS = {
  name: 120,
  level: { min: 1, max: 20 },
  score: { min: 3, max: 20 },
  comment: 1000,
} as const;

/** What `derive_sheet` computes from the base facts. Never authored by a player. */
export interface SheetDerived {
  ability_modifiers: Record<Ability, number>;
  proficiency_bonus: number;
  saving_throw_bonuses: Record<Ability, number>;
  attack_bonuses: { melee: number; ranged: number };
  unarmored_armor_class: number;
  max_hit_points: number;
  hit_die: string;
  corpus: { ancestry_entity_id: string; class_entity_id: string };
}

export interface SheetVersion extends Omit<
  CharacterSheetView,
  "sheet" | "derived"
> {
  sheet: CharacterSheetV1;
  /** Null when the stored row carries no calculated values. */
  derived: SheetDerived | null;
}

export interface SheetHistory extends Omit<
  CharacterSheetHistoryView,
  "versions"
> {
  /** Newest first. */
  versions: SheetVersion[];
}

// The schema types `sheet` and `derived` as open objects; this is the one cast.
const toVersion = (view: CharacterSheetView): SheetVersion => ({
  ...view,
  sheet: view.sheet as unknown as CharacterSheetV1,
  derived:
    "proficiency_bonus" in view.derived
      ? (view.derived as unknown as SheetDerived)
      : null,
});

export const keys = {
  sheets: (campaignId: string, characterId: string) =>
    [...lobbyKeys.characters(campaignId), characterId, "sheets"] as const,
};

interface CharacterRef {
  campaignId: string;
  characterId: string;
}

interface VersionRef extends CharacterRef {
  versionId: string;
}

const characterParams = (ref: CharacterRef) => ({
  campaign_id: ref.campaignId,
  player_character_id: ref.characterId,
});

const versionParams = (ref: VersionRef) => ({
  ...characterParams(ref),
  sheet_version_id: ref.versionId,
});

export const useSheetHistory = (campaignId: string, characterId: string) =>
  useQuery({
    queryKey: keys.sheets(campaignId, characterId),
    queryFn: async (): Promise<SheetHistory> => {
      const history = await api(
        "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets",
        { params: characterParams({ campaignId, characterId }) },
      );
      return { ...history, versions: history.versions.map(toVersion) };
    },
  });

/** Updates the open draft, or starts the next version when there is none. */
export const saveDraft = (
  input: CharacterRef & { versionId: string | null; sheet: CharacterSheetV1 },
) =>
  input.versionId
    ? api(
        "PATCH /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}",
        {
          params: versionParams({ ...input, versionId: input.versionId }),
          body: input.sheet,
        },
      )
    : api(
        "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/drafts",
        { params: characterParams(input), body: input.sheet },
      );

export const submitDraft = (input: VersionRef) =>
  api(
    "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/submit",
    { params: versionParams(input) },
  );

export const decideSheet = (
  input: VersionRef & {
    decision: "approve" | "return";
    comment: string | null;
  },
) =>
  api(
    input.decision === "approve"
      ? "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/approve"
      : "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/return",
    { params: versionParams(input), body: { comment: input.comment } },
  );
