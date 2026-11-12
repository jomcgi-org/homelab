import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Grid } from "@astryxdesign/core/Grid";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import {
  MetadataList,
  MetadataListItem,
} from "@astryxdesign/core/MetadataList";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import type { CharacterSheetV1 } from "../../api/schema";
import {
  ABILITIES,
  BOUNDS,
  keys,
  saveDraft,
  submitDraft,
  useSheetOptions,
  type Ability,
  type SheetOptions,
  type SheetVersion,
} from "../../api/sheets";
import { Loaded } from "../../app/Loaded";
import { useAction } from "../../app/useAction";
import { previewSheet } from "../../lib/sheet-preview.js";

interface SheetFormProps {
  campaignId: string;
  characterId: string;
  characterName: string;
  /** The open draft, when there is one. Saving without it starts a version. */
  draft: SheetVersion | null;
  /** The facts the form starts from: the latest version's, or none. */
  base: CharacterSheetV1 | null;
}

// A long list gets a search box inside the same control.
const SEARCH_FROM = 9;

const signed = (value: number) => (value >= 0 ? `+${value}` : String(value));

const facts = (base: CharacterSheetV1 | null): CharacterSheetV1 => {
  const score = (ability: Ability) => base?.ability_scores?.[ability] ?? 10;
  return {
    schema_version: 1,
    ancestry: base?.ancestry ?? "",
    class_name: base?.class_name ?? "",
    level: base?.level ?? 1,
    ability_scores: {
      strength: score("strength"),
      dexterity: score("dexterity"),
      constitution: score("constitution"),
      intelligence: score("intelligence"),
      wisdom: score("wisdom"),
      charisma: score("charisma"),
    },
  };
};

/**
 * Upper then lower case approximates the backend's casefold, so "Straße"
 * and "strasse" match here as they do there.
 */
const fold = (value: string) => value.toUpperCase().toLowerCase();

/** The option a saved name refers to. The backend matches names without case. */
const named = <T extends { name: string }>(options: T[], name: string) =>
  options.find((option) => fold(option.name) === fold(name)) ?? null;

/** The corpus facts behind a choice, as one plain line. Absent facts are left out. */
const factLine = (parts: Array<[label: string, value: string | null]>) =>
  parts
    .filter(([, value]) => value)
    .map(([label, value]) => `${label} ${value}.`)
    .join(" ");

const classFacts = (option: SheetOptions["classes"][number]) =>
  factLine([
    ["Hit die", option.hit_die],
    ["Saving throws:", option.saves],
    ["Primary ability:", option.primary_ability],
  ]);

const ancestryFacts = (option: SheetOptions["ancestries"][number]) =>
  factLine([
    ["Size", option.size],
    ["Speed", option.speed],
  ]);

/**
 * The player's editor: base facts only, chosen from what the campaign's rules
 * hold for this character. Grimoire calculates the rest.
 */
export function SheetForm(props: SheetFormProps) {
  const options = useSheetOptions(props.campaignId, props.characterId);
  return (
    <Loaded query={options} what="the choices for this sheet">
      {(data) => <Editor {...props} options={data} />}
    </Loaded>
  );
}

function Editor({
  campaignId,
  characterId,
  characterName,
  draft,
  base,
  options,
}: SheetFormProps & { options: SheetOptions }) {
  const [saved] = useState(() => facts(base));
  const [sheet, setSheet] = useState(saved);
  const [checked, setChecked] = useState(false);
  const invalidate = [keys.sheets(campaignId, characterId)];
  const save = useAction(saveDraft, { invalidate, success: "Draft saved." });
  const submit = useAction(submitDraft, {
    invalidate,
    success: "Submitted to the DM.",
  });

  const ancestry = named(options.ancestries, sheet.ancestry);
  const characterClass = named(options.classes, sheet.class_name);
  const preview = previewSheet(sheet, characterClass);
  const complete = ancestry !== null && characterClass !== null;
  const unsaved = JSON.stringify(sheet) !== JSON.stringify(saved);

  const choice = (
    what: "ancestry" | "class",
    article: "an" | "a",
    count: number,
    name: string,
    chosen: boolean,
  ) => {
    const error = (message: string) => ({ type: "error", message }) as const;
    if (count === 0)
      return error(
        `No ${what} is available to ${characterName}. Ask your DM to share ${article} ${what} with ${characterName}.`,
      );
    if (name && !chosen)
      return error(
        `${name} is no longer available to ${characterName}. Choose another ${what}.`,
      );
    return checked && !chosen ? error(`Choose ${article} ${what}.`) : undefined;
  };
  const noChoices =
    options.ancestries.length === 0 || options.classes.length === 0;

  const setScore = (ability: Ability) => (value: number) =>
    setSheet({
      ...sheet,
      ability_scores: { ...sheet.ability_scores, [ability]: value },
    });
  const onSave = () => {
    setChecked(true);
    if (!complete) return;
    save.run({
      campaignId,
      characterId,
      versionId: draft?.id ?? null,
      sheet,
    });
  };
  const { saving_throw_bonuses: saves, proficient_saves: proficient } = preview;
  const classItems = saves &&
    proficient && [
      <MetadataListItem key="hp" label="Maximum hit points">
        {preview.max_hit_points}
      </MetadataListItem>,
      <MetadataListItem key="die" label="Hit die">
        {preview.hit_die}
      </MetadataListItem>,
      ...ABILITIES.map(({ name, label }) => (
        <MetadataListItem key={name} label={`${label} save`}>
          {signed(saves[name])}
          {proficient.includes(name) && ", proficient"}
        </MetadataListItem>
      )),
    ];
  // One primary action: submit once the draft is saved, save until then.
  const readyToSubmit = draft !== null && !unsaved && complete;
  return (
    <VStack gap={4}>
      <Grid columns={{ minWidth: 220, max: 2, repeat: "fit" }} gap={3}>
        <VStack gap={1}>
          <Selector
            label="Ancestry"
            placeholder="Choose an ancestry"
            options={options.ancestries.map(({ name }) => name)}
            value={ancestry?.name}
            onChange={(name) => setSheet({ ...sheet, ancestry: name })}
            status={choice(
              "ancestry",
              "an",
              options.ancestries.length,
              sheet.ancestry,
              ancestry !== null,
            )}
            statusVariant="detached"
            hasSearch={options.ancestries.length >= SEARCH_FROM}
            isDisabled={options.ancestries.length === 0}
            width="100%"
          />
          {ancestry && ancestryFacts(ancestry) && (
            <Text type="supporting">{ancestryFacts(ancestry)}</Text>
          )}
        </VStack>
        <VStack gap={1}>
          <Selector
            label="Class"
            placeholder="Choose a class"
            options={options.classes.map(({ name }) => name)}
            value={characterClass?.name}
            onChange={(name) => setSheet({ ...sheet, class_name: name })}
            status={choice(
              "class",
              "a",
              options.classes.length,
              sheet.class_name,
              characterClass !== null,
            )}
            statusVariant="detached"
            hasSearch={options.classes.length >= SEARCH_FROM}
            isDisabled={options.classes.length === 0}
            width="100%"
          />
          {characterClass && classFacts(characterClass) && (
            <Text type="supporting">{classFacts(characterClass)}</Text>
          )}
        </VStack>
      </Grid>
      <NumberInput
        label="Level"
        value={sheet.level}
        onChange={(level) => setSheet({ ...sheet, level })}
        min={BOUNDS.level.min}
        max={BOUNDS.level.max}
        isIntegerOnly
        width={96}
      />
      <VStack gap={2}>
        <VStack gap={1}>
          <Heading level={4}>Ability scores</Heading>
          <Text type="supporting">
            Whole numbers from {BOUNDS.score.min} to {BOUNDS.score.max}.
          </Text>
        </VStack>
        <Grid columns={3} gap={3}>
          {ABILITIES.map(({ name, label }) => (
            <NumberInput
              key={name}
              label={label}
              description={`Modifier ${signed(preview.ability_modifiers[name])}`}
              value={sheet.ability_scores[name]}
              onChange={setScore(name)}
              min={BOUNDS.score.min}
              max={BOUNDS.score.max}
              isIntegerOnly
            />
          ))}
        </Grid>
      </VStack>
      <VStack gap={2}>
        <VStack gap={1}>
          <Heading level={4}>What Grimoire will calculate</Heading>
          <Text type="supporting">
            A preview from your choices. Grimoire calculates the sheet itself
            when you save.
          </Text>
        </VStack>
        <MetadataList columns={2}>
          <MetadataListItem label="Proficiency bonus">
            {signed(preview.proficiency_bonus)}
          </MetadataListItem>
          <MetadataListItem label="Armour class, unarmoured">
            {preview.unarmored_armor_class}
          </MetadataListItem>
          {classItems}
        </MetadataList>
        {!classItems && options.classes.length > 0 && (
          <Text type="body" color="secondary">
            {preview.refusal
              ? `Grimoire cannot calculate hit points or saving throws for ${characterClass?.name}: ${preview.refusal}. Choose another class or ask your DM to fix the entry.`
              : "Choose a class to see hit points and saving throws."}
          </Text>
        )}
      </VStack>
      <HStack gap={2} wrap="wrap">
        <Button
          label={draft ? "Save draft" : "Start new draft"}
          variant={readyToSubmit ? "secondary" : "primary"}
          isLoading={save.isPending}
          isDisabled={noChoices}
          onClick={onSave}
        />
        {draft && (
          <Button
            label="Submit to DM"
            variant={readyToSubmit ? "primary" : "secondary"}
            isLoading={submit.isPending}
            isDisabled={!readyToSubmit || save.isPending}
            onClick={() =>
              submit.run({ campaignId, characterId, versionId: draft.id })
            }
          />
        )}
      </HStack>
      {draft && (
        <Text type="supporting">
          {readyToSubmit
            ? `Submitting locks version ${draft.version} for DM review.`
            : `Save your changes before submitting version ${draft.version}.`}
        </Text>
      )}
    </VStack>
  );
}
