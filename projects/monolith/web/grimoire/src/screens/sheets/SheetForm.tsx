import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
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
import { TextArea } from "@astryxdesign/core/TextArea";
import { useToast } from "@astryxdesign/core/Toast";
import { useAnnounce, useClipboard } from "@astryxdesign/core/hooks";
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
import {
  keepSheetRecovery,
  restoreSheetRecovery,
  sheetRecoveryEpoch,
} from "../../lib/sheet-recovery";
import {
  describeSheet,
  fieldValue,
  mergeSheet,
  sheetFields,
  takeSavedFields,
  withNumericText,
  type NumericText,
  type SheetField,
} from "./merge";

interface SheetFormProps {
  accountId: string;
  campaignId: string;
  characterId: string;
  characterName: string;
  /** Latest authoritative version, including its status and save revision. */
  version: SheetVersion | null;
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
    <Loaded query={options} what="the choices for this sheet" requireFresh>
      {(data) => <Editor {...props} options={data} />}
    </Loaded>
  );
}

function Editor({
  accountId,
  campaignId,
  characterId,
  characterName,
  version,
  options,
}: SheetFormProps & { options: SheetOptions }) {
  const [seenVersion, setSeenVersion] = useState(version);
  const [saved, setSaved] = useState(() => facts(version?.sheet ?? null));
  const scope = { accountId, campaignId, characterId };
  const [recovery] = useState(() => restoreSheetRecovery(scope, saved));
  const [recovered, setRecovered] = useState(recovery !== null);
  const [epoch] = useState(() => sheetRecoveryEpoch(accountId));
  const [writer] = useState(() => crypto.randomUUID());
  const claimRecovery = useRef(recovery !== null);
  const editedAt = useRef(recovery?.editedAt ?? 0);
  const [sheet, setSheet] = useState(recovery?.sheet ?? saved);
  const [conflicts, setConflicts] = useState<SheetField[]>(
    recovery?.conflicts ?? [],
  );
  const [numericText, setNumericText] = useState<NumericText>(
    recovery?.numericText ?? {},
  );
  const [durable, setDurable] = useState(true);
  const [checked, setChecked] = useState(false);
  const local = withNumericText(sheet, numericText);
  // Compare both sets of changes with the previous saved snapshot. A poll
  // must never turn untouched, old fields into deliberate local edits.
  if (seenVersion !== version) {
    const next = facts(version?.sheet ?? null);
    const merged = mergeSheet(
      saved,
      local,
      next,
      conflicts,
      Object.keys(numericText) as SheetField[],
    );
    setSeenVersion(version);
    setSaved(next);
    setSheet(merged.sheet);
    setConflicts(merged.conflicts);
  }
  const client = useQueryClient();
  const announce = useAnnounce();
  const clipboard = useClipboard();
  const toast = useToast();
  const draft = version?.status === "draft" ? version : null;
  const locked = version?.status === "submitted";
  const invalidate = [keys.sheets(campaignId, characterId)];
  const save = useAction(saveDraft, { invalidate, success: "Draft saved." });
  const submit = useAction(submitDraft, {
    invalidate,
    success: "Submitted to the DM.",
  });

  const ancestry = named(options.ancestries, local.ancestry);
  const characterClass = named(options.classes, local.class_name);
  const preview = previewSheet(local, characterClass);
  const complete = ancestry !== null && characterClass !== null;
  const unsaved =
    JSON.stringify(local) !== JSON.stringify(saved) ||
    Object.entries(numericText).some(
      ([name, value]) =>
        value !== String(fieldValue(saved, name as SheetField)),
    );
  // Persist synchronously in input handlers, before an access-change render
  // can unmount this editor. Polls may update merge bases, never retention.
  const edit = (next: CharacterSheetV1, typing: NumericText = numericText) => {
    const values = withNumericText(next, typing);
    if (
      sheetFields.some(
        ({ name }) => fieldValue(values, name) !== fieldValue(local, name),
      ) ||
      Object.entries(typing).some(
        ([name, value]) => value !== numericText[name as keyof NumericText],
      )
    )
      editedAt.current = Date.now();
    setDurable(
      keepSheetRecovery(
        scope, epoch, editedAt.current, saved, next, typing, conflicts, writer,
      ),
    );
    setSheet(next);
    setNumericText(typing);
  };
  useLayoutEffect(() => {
    setDurable(
      keepSheetRecovery(
        { accountId, campaignId, characterId },
        epoch, editedAt.current, saved, sheet, numericText, conflicts, writer,
        claimRecovery.current,
      ),
    );
    claimRecovery.current = false;
    if (!unsaved) setRecovered(false);
  }, [
    accountId, campaignId, characterId, epoch, writer,
    saved, sheet, numericText, conflicts, unsaved,
  ]);
  useEffect(() => {
    if (locked && unsaved)
      announce(
        "This version was submitted. Your unsaved changes are kept for copying or a later return.",
      );
    else if (conflicts.length)
      announce(
        "Another tab changed the same sheet fields. Resolve the changes before saving.",
      );
    else if (recovered && unsaved)
      announce(
        "Recovered your unsaved edits for this character. Review them before saving.",
      );
  }, [announce, locked, unsaved, conflicts.length, recovered]);

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

  const clearNumeric = (name: "level" | Ability) =>
    setNumericText((current) => {
      const next = { ...current };
      delete next[name];
      return next;
    });
  const setScore = (ability: Ability) => (value: number) => {
    const typing = { ...numericText };
    delete typing[ability];
    edit(
      {
        ...local,
        ability_scores: { ...local.ability_scores, [ability]: value },
      },
      typing,
    );
  };
  const onSave = async () => {
    setChecked(true);
    if (
      !complete ||
      locked ||
      conflicts.length ||
      save.isPending ||
      submit.isPending
    )
      return;
    const result = await save.run({
      campaignId,
      characterId,
      versionId: draft?.id ?? null,
      sheet: local,
      expectedUpdatedAt: draft?.updated_at ?? null,
    });
    // A stale-revision or status conflict requires fresh facts, not an
    // automatic retry with an old payload. Keep the local buffer meanwhile.
    if (!result)
      await client.invalidateQueries({
        queryKey: keys.sheets(campaignId, characterId),
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
  const readyToSubmit =
    draft !== null && !unsaved && complete && conflicts.length === 0;
  if (locked) {
    if (!unsaved) return null;
    const copyText = describeSheet(local, numericText);
    return (
      <VStack gap={3} as="section" aria-label="Unsaved sheet changes">
        <Heading level={4}>Your unsaved changes are kept</Heading>
        <Text type="body">
          This version was submitted in another tab. Copy your edits before
          leaving, or continue editing if the DM returns it.
        </Text>
        <TextArea
          label="Your unsaved sheet changes"
          value={copyText}
          isReadOnly
          rows={10}
        />
        <HStack gap={2} wrap="wrap">
          <Button
            label="Copy my unsaved changes"
            onClick={async () => {
              const copied = await clipboard.copy(copyText);
              toast(
                copied
                  ? { body: "Unsaved sheet changes copied." }
                  : {
                      body: "Could not copy. Select the text above and copy it manually.",
                      type: "error",
                    },
              );
            }}
          />
          <Button
            label="Discard my unsaved changes"
            variant="secondary"
            onClick={() => {
              setSheet(saved);
              setNumericText({});
              setConflicts([]);
            }}
          />
        </HStack>
      </VStack>
    );
  }
  // Observe native input in the bubble phase. A capture-phase parent update
  // can restore the old value before NumberInput receives its change event.
  return (
    <VStack
      gap={4}
      onInput={(event: FormEvent<HTMLDivElement>) => {
        const input = event.target;
        if (!(input instanceof HTMLInputElement)) return;
        const name = input.name;
        const text = input.value;
        if (
          name === "level" ||
          ABILITIES.some((ability) => ability.name === name)
        )
          edit(sheet, { ...numericText, [name]: text });
      }}
    >
      {recovered && unsaved && (
        <VStack gap={2} as="section" aria-label="Recovered sheet changes">
          <Text type="body">
            Recovered your unsaved edits for this character. Review them before saving.
          </Text>
          {Object.keys(numericText).length > 0 && (
            <TextArea
              label="Recovered sheet input"
              value={describeSheet(local, numericText)}
              isReadOnly
              rows={10}
            />
          )}
          <Button
            label="Discard recovered changes"
            variant="secondary"
            onClick={() => {
              setSheet(saved);
              setNumericText({});
              setConflicts([]);
              setRecovered(false);
            }}
          />
        </VStack>
      )}
      {unsaved && !durable && (
        <Text type="body">
          This browser could not keep a recovery copy. Save your changes before leaving.
        </Text>
      )}
      {conflicts.length > 0 && (
        <VStack gap={2} as="section" aria-label="Resolve sheet changes">
          <Text type="body">
            This sheet changed in another tab. Choose which values to keep
            before saving.
          </Text>
          {sheetFields
            .filter(({ name }) => conflicts.includes(name))
            .map(({ name, label }) => (
              <Text key={name} type="body">
                {label}: your edit {fieldValue(local, name)}; saved value{" "}
                {fieldValue(saved, name)}.
              </Text>
            ))}
          <HStack gap={2} wrap="wrap">
            <Button
              label="Keep my conflicting edits"
              onClick={() => setConflicts([])}
            />
            <Button
              label="Use saved values"
              variant="secondary"
              onClick={() => {
                setSheet(takeSavedFields(local, saved, conflicts));
                setNumericText((current) =>
                  Object.fromEntries(
                    Object.entries(current).filter(
                      ([name]) => !conflicts.includes(name as SheetField),
                    ),
                  ),
                );
                setConflicts([]);
              }}
            />
          </HStack>
        </VStack>
      )}
      <Grid columns={{ minWidth: 220, max: 2, repeat: "fit" }} gap={3}>
        <VStack gap={1}>
          <Selector
            label="Ancestry"
            placeholder="Choose an ancestry"
            options={options.ancestries.map(({ name }) => name)}
            value={ancestry?.name}
            onChange={(name) => edit({ ...sheet, ancestry: name })}
            status={choice(
              "ancestry",
              "an",
              options.ancestries.length,
              sheet.ancestry,
              ancestry !== null,
            )}
            statusVariant="detached"
            hasSearch={options.ancestries.length >= SEARCH_FROM}
            isDisabled={
              options.ancestries.length === 0 ||
              save.isPending ||
              submit.isPending
            }
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
            onChange={(name) => edit({ ...sheet, class_name: name })}
            status={choice(
              "class",
              "a",
              options.classes.length,
              sheet.class_name,
              characterClass !== null,
            )}
            statusVariant="detached"
            hasSearch={options.classes.length >= SEARCH_FROM}
            isDisabled={
              options.classes.length === 0 || save.isPending || submit.isPending
            }
            width="100%"
          />
          {characterClass && classFacts(characterClass) && (
            <Text type="supporting">{classFacts(characterClass)}</Text>
          )}
        </VStack>
      </Grid>
      <NumberInput
        label="Level"
        htmlName="level"
        onBlur={() => clearNumeric("level")}
        value={sheet.level}
        onChange={(level) => {
          const typing = { ...numericText };
          delete typing.level;
          edit({ ...local, level }, typing);
        }}
        isDisabled={save.isPending || submit.isPending}
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
              htmlName={name}
              onBlur={() => clearNumeric(name)}
              description={`Modifier ${signed(preview.ability_modifiers[name])}`}
              value={sheet.ability_scores[name]}
              onChange={setScore(name)}
              isDisabled={save.isPending || submit.isPending}
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
          isDisabled={
            noChoices ||
            conflicts.length > 0 ||
            save.isPending ||
            submit.isPending
          }
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
