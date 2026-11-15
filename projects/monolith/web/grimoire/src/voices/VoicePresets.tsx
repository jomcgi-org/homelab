import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@astryxdesign/core/Button";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { List, ListItem } from "@astryxdesign/core/List";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { errorMessage } from "../api/client";
import { sessionKeys } from "../api/session";
import {
  deleteVoice,
  saveVoice,
  searchNpcs,
  SPEAKER_KEY,
  VOICE_LIMITS,
  voiceKeys,
  type NpcMatch,
} from "../api/voices";
import { useAction } from "../app/useAction";
import { FREE_LABEL, presetSpeakers } from "../lib/read-aloud.js";
import type { VoicePreset } from "./voices";

export interface VoicePresetsProps {
  campaignId: string;
  presets: VoicePreset[];
}

interface Speaker {
  key: string;
  name: string;
  unavailable?: boolean;
}

const NARRATOR = "narrator";
// Colons never appear in speaker keys, so this cannot shadow a free label.
const FIND = "npc:find";
const NPC_NAME_MAX = 200;

const splitNames = (text: string) =>
  text
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);

const within = (value: number | null, range: { min: number; max: number }) =>
  value !== null && value >= range.min && value <= range.max;

/** The DM's saved read-aloud voices for the narrator and NPCs. */
export function VoicePresets({ campaignId, presets }: VoicePresetsProps) {
  const speakers = presetSpeakers(presets, {
    includeUnavailable: true,
  }) as Speaker[];
  const [choice, setChoice] = useState(NARRATOR);
  const [picked, setPicked] = useState<Speaker | null>(null);
  const [freeLabel, setFreeLabel] = useState("");

  const pickedOption =
    picked && !speakers.some((row) => row.key === picked.key) ? [picked] : [];
  const options = [
    { value: NARRATOR, label: "Narrator" },
    ...[...pickedOption, ...speakers].map((row) => ({
      value: row.key,
      label: row.name,
    })),
    { value: FIND, label: "Find an NPC" },
    { value: FREE_LABEL, label: "Free label" },
  ];
  // A deleted preset leaves the picker; fall back rather than keep its key.
  const selected = options.some((option) => option.value === choice)
    ? choice
    : NARRATOR;
  const key = selected === FREE_LABEL ? freeLabel.trim() : selected;

  return (
    <Collapsible
      defaultIsOpen={false}
      trigger={<Text type="label">Voice presets</Text>}
    >
      <VStack gap={4}>
        <Text type="supporting" as="p">
          Voice names vary by device. Matching names take priority, then
          language, then the device default. Hints and free labels are visible
          to campaign members; use an NPC key for hidden identities.
        </Text>
        <Selector
          label="Preset speaker"
          options={options}
          value={selected}
          onChange={setChoice}
          width={280}
        />
        {selected === FIND ? (
          <NpcFinder
            campaignId={campaignId}
            onPick={(npc) => {
              // Saved entity keys are canonical lowercase ids.
              const speaker = { key: npc.id.toLowerCase(), name: npc.name };
              setPicked(speaker);
              setChoice(speaker.key);
            }}
          />
        ) : (
          <>
            {selected === FREE_LABEL && (
              <TextInput
                label="Preset label"
                description="Letters, numbers, spaces and _ ' . - only."
                value={freeLabel}
                onChange={(value) => setFreeLabel(value.slice(0, 64))}
                status={
                  !key || SPEAKER_KEY.test(key)
                    ? undefined
                    : {
                        type: "error",
                        message: "Choose a narrator, NPC or speaker label.",
                      }
                }
                width={280}
              />
            )}
            <PresetForm
              campaignId={campaignId}
              speakerKey={SPEAKER_KEY.test(key) ? key : ""}
              stored={presets.find((row) => row.speaker_key === key) ?? null}
              unavailable={speakers.some(
                (row) => row.key === key && row.unavailable,
              )}
            />
          </>
        )}
      </VStack>
    </Collapsible>
  );
}

interface PresetFormProps {
  campaignId: string;
  /** Empty until the DM has named a valid speaker. */
  speakerKey: string;
  stored: VoicePreset | null;
  /** The NPC left the DM's view: its preset can only be deleted. */
  unavailable: boolean;
}

function PresetForm({
  campaignId,
  speakerKey,
  stored,
  unavailable,
}: PresetFormProps) {
  const [lang, setLang] = useState(stored?.voice_hint?.lang ?? "");
  const [names, setNames] = useState(
    stored?.voice_hint?.names?.join(", ") ?? "",
  );
  const [rate, setRate] = useState<number | null>(stored?.rate ?? 1);
  const [pitch, setPitch] = useState<number | null>(stored?.pitch ?? 1);

  // Compared by value: each poll hands over a new array, which must not
  // reload the form over unsaved edits. A save or delete changes the stored
  // preset, so the form still reloads then.
  const storedValue = JSON.stringify(stored);
  useEffect(() => {
    const preset = JSON.parse(storedValue) as VoicePreset | null;
    setLang(preset?.voice_hint?.lang ?? "");
    setNames(preset?.voice_hint?.names?.join(", ") ?? "");
    setRate(preset?.rate ?? 1);
    setPitch(preset?.pitch ?? 1);
  }, [storedValue]);

  const invalidate = [sessionKeys.voices(campaignId)];
  const save = useAction(saveVoice, { invalidate, success: "Voice saved." });
  const remove = useAction(deleteVoice, {
    invalidate,
    success: "Voice deleted.",
  });
  const busy = save.isPending || remove.isPending;

  const nameList = splitNames(names);
  const namesValid =
    nameList.length <= VOICE_LIMITS.names &&
    nameList.every((name) => name.length <= VOICE_LIMITS.name);
  const valid =
    namesValid &&
    within(rate, VOICE_LIMITS.rate) &&
    within(pitch, VOICE_LIMITS.pitch);

  return (
    <VStack gap={4}>
      {unavailable ? (
        <Text type="body">
          This NPC is no longer in the campaign. Delete its preset.
        </Text>
      ) : (
        <>
          <HStack gap={3} wrap="wrap" vAlign="start">
            <TextInput
              label="Language hint"
              placeholder="en-GB"
              value={lang}
              onChange={(value) => setLang(value.slice(0, VOICE_LIMITS.lang))}
              width={160}
            />
            <TextInput
              label="Preferred voice names"
              description="Up to eight name substrings, separated by commas."
              placeholder="English, North"
              value={names}
              onChange={setNames}
              status={
                namesValid
                  ? undefined
                  : {
                      type: "error",
                      message: "Use at most eight names of 64 characters.",
                    }
              }
              width={280}
            />
          </HStack>
          <HStack gap={3} wrap="wrap" vAlign="start">
            <NumberInput
              label="Voice rate"
              value={rate}
              onChange={setRate}
              min={VOICE_LIMITS.rate.min}
              max={VOICE_LIMITS.rate.max}
              step={0.1}
              width={120}
            />
            <NumberInput
              label="Voice pitch"
              value={pitch}
              onChange={setPitch}
              min={VOICE_LIMITS.pitch.min}
              max={VOICE_LIMITS.pitch.max}
              step={0.1}
              width={120}
            />
          </HStack>
        </>
      )}
      <HStack gap={2} wrap="wrap">
        {!unavailable && (
          <Button
            label="Save voice"
            variant="primary"
            isLoading={save.isPending}
            isDisabled={busy || !speakerKey || !valid}
            onClick={() =>
              save.run({
                campaignId,
                speakerKey,
                voice: {
                  voice_hint: { lang: lang.trim() || null, names: nameList },
                  rate: rate!,
                  pitch: pitch!,
                },
              })
            }
          />
        )}
        <Button
          label="Delete voice"
          variant="ghost"
          isDisabled={busy || !speakerKey || stored === null}
          onClick={() => remove.run({ campaignId, speakerKey })}
        />
      </HStack>
    </VStack>
  );
}

interface NpcFinderProps {
  campaignId: string;
  onPick: (npc: NpcMatch) => void;
}

/** Finds an NPC by name so a preset can be keyed to it. */
function NpcFinder({ campaignId, onPick }: NpcFinderProps) {
  const [name, setName] = useState("");
  const [asked, setAsked] = useState("");
  const search = useQuery({
    queryKey: voiceKeys.npcs(campaignId, asked),
    queryFn: () => searchNpcs(campaignId, asked),
    enabled: asked !== "",
  });
  const find = () => name.trim() && setAsked(name.trim());
  const found = asked ? search.data : undefined;

  // Same-named NPCs show where each comes from, when the result says.
  const lower = (found?.items ?? []).map((npc) => npc.name.toLowerCase());
  const source = (npc: NpcMatch) =>
    lower.indexOf(npc.name.toLowerCase()) !==
    lower.lastIndexOf(npc.name.toLowerCase())
      ? npc.source_book || (npc.source_type === "homebrew" ? "Homebrew" : "")
      : "";

  return (
    <VStack gap={3}>
      <HStack gap={2} wrap="wrap" vAlign="end">
        <TextInput
          type="search"
          label="NPC name"
          value={name}
          onChange={(value) => setName(value.slice(0, NPC_NAME_MAX))}
          onEnter={find}
          status={
            search.isError
              ? {
                  type: "error",
                  message:
                    errorMessage(search.error) || "Could not search NPCs.",
                }
              : undefined
          }
          width={280}
        />
        <Button
          label="Find NPC"
          isLoading={search.isFetching}
          isDisabled={!name.trim()}
          onClick={find}
        />
      </HStack>
      {found && found.items.length === 0 && (
        <Text type="body">No NPC matches that name.</Text>
      )}
      {found && found.items.length > 0 && (
        <List hasDividers header="Matching NPCs">
          {found.items.map((npc) => (
            <ListItem
              key={npc.id}
              label={npc.name}
              description={source(npc) || undefined}
              endContent={
                <Button
                  label={`Use ${npc.name}${source(npc) ? ` (${source(npc)})` : ""}`}
                  size="sm"
                  onClick={() => onPick(npc)}
                >
                  Use
                </Button>
              }
            />
          ))}
        </List>
      )}
      {found && found.total > found.items.length && (
        <Text type="supporting">
          Showing {found.items.length} of {found.total}. Type more of the name.
        </Text>
      )}
    </VStack>
  );
}
