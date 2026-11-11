import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { FormLayout } from "@astryxdesign/core/FormLayout";
import { Grid } from "@astryxdesign/core/Grid";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { CharacterSheetV1 } from "../../api/schema";
import {
  ABILITIES,
  BOUNDS,
  keys,
  saveDraft,
  submitDraft,
  type Ability,
  type SheetVersion,
} from "../../api/sheets";
import { useAction } from "../../app/useAction";

interface SheetFormProps {
  campaignId: string;
  characterId: string;
  /** The open draft, when there is one. Saving without it starts a version. */
  draft: SheetVersion | null;
  /** The facts the form starts from: the latest version's, or none. */
  base: CharacterSheetV1 | null;
}

// The backend collapses runs of whitespace; doing it here keeps "unsaved
// changes" honest after a save round-trips.
const collapse = (value: string) => value.trim().split(/\s+/).join(" ");

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

const normalized = (sheet: CharacterSheetV1): CharacterSheetV1 => ({
  ...sheet,
  ancestry: collapse(sheet.ancestry),
  class_name: collapse(sheet.class_name),
});

const required = (value: string, message: string) =>
  collapse(value) ? undefined : ({ type: "error", message } as const);

/** The player's editor: base facts only. Grimoire calculates the rest. */
export function SheetForm({
  campaignId,
  characterId,
  draft,
  base,
}: SheetFormProps) {
  const [saved] = useState(() => normalized(facts(base)));
  const [sheet, setSheet] = useState(saved);
  const [checked, setChecked] = useState(false);
  const invalidate = [keys.sheets(campaignId, characterId)];
  const save = useAction(saveDraft, { invalidate, success: "Draft saved." });
  const submit = useAction(submitDraft, {
    invalidate,
    success: "Submitted to the DM.",
  });
  const errors = {
    ancestry: required(sheet.ancestry, "Enter an ancestry."),
    class_name: required(sheet.class_name, "Enter a class."),
  };
  const unsaved = JSON.stringify(normalized(sheet)) !== JSON.stringify(saved);
  const setName = (field: "ancestry" | "class_name") => (value: string) =>
    setSheet({ ...sheet, [field]: value.slice(0, BOUNDS.name) });
  const setScore = (ability: Ability) => (value: number) =>
    setSheet({
      ...sheet,
      ability_scores: { ...sheet.ability_scores, [ability]: value },
    });
  const onSave = () => {
    setChecked(true);
    if (errors.ancestry || errors.class_name) return;
    save.run({
      campaignId,
      characterId,
      versionId: draft?.id ?? null,
      sheet: normalized(sheet),
    });
  };
  return (
    <VStack gap={4}>
      <FormLayout>
        <FormLayout direction="horizontal">
          <TextInput
            label="Ancestry"
            value={sheet.ancestry}
            onChange={setName("ancestry")}
            status={checked ? errors.ancestry : undefined}
            isRequired
          />
          <TextInput
            label="Class"
            value={sheet.class_name}
            onChange={setName("class_name")}
            status={checked ? errors.class_name : undefined}
            isRequired
          />
        </FormLayout>
        <NumberInput
          label="Level"
          value={sheet.level}
          onChange={(level) => setSheet({ ...sheet, level })}
          min={BOUNDS.level.min}
          max={BOUNDS.level.max}
          isIntegerOnly
          width={96}
        />
      </FormLayout>
      <VStack gap={2}>
        <Heading level={4}>Base ability scores</Heading>
        <Grid columns={3} gap={3}>
          {ABILITIES.map(({ name, label }) => (
            <NumberInput
              key={name}
              label={label}
              value={sheet.ability_scores[name]}
              onChange={setScore(name)}
              min={BOUNDS.score.min}
              max={BOUNDS.score.max}
              isIntegerOnly
            />
          ))}
        </Grid>
      </VStack>
      <HStack gap={2} wrap="wrap">
        <Button
          label={draft ? "Save draft" : "Start new draft"}
          isLoading={save.isPending}
          onClick={onSave}
        />
        {draft && (
          <Button
            label="Submit to DM"
            variant="primary"
            isLoading={submit.isPending}
            isDisabled={unsaved || save.isPending}
            onClick={() =>
              submit.run({ campaignId, characterId, versionId: draft.id })
            }
          />
        )}
      </HStack>
      {draft && (
        <Text type="supporting">
          {unsaved
            ? `Save your changes before submitting version ${draft.version}.`
            : `Submitting locks version ${draft.version} for DM review.`}
        </Text>
      )}
    </VStack>
  );
}
