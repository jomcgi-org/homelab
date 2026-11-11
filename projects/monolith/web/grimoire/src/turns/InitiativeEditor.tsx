import { useEffect, useRef, useState } from "react";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { RadioList, RadioListItem } from "@astryxdesign/core/RadioList";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useToast } from "@astryxdesign/core/Toast";
import type { MemberView } from "../api/schema";
import { sessionKeys, type SessionEvent, type TurnOrder } from "../api/session";
import {
  advanceInitiative,
  endInitiative,
  MAX_TURN_ENTRIES,
  MAX_TURN_LABEL,
  setInitiative,
} from "../api/turns";
import { useAction } from "../app/useAction";
import type { TableCharacter } from "../screens/session/table";
import "./turns.css";

export interface InitiativeEditorProps {
  campaignId: string;
  sessionId: string;
  initiative: TurnOrder;
  characters: TableCharacter[];
  events: SessionEvent[];
  members: MemberView[];
}

type HiddenDisplay = "mask" | "omit";

/** One entry of the DM's unsaved draft. */
interface Row {
  /** Stable across reorders, so the turn follows its entry. */
  key: number;
  label: string;
  /** Empty for an NPC. */
  characterId: string;
  /** Kept as typed, so a half-typed number is not rewritten under the DM. */
  initiative: string;
  hidden: boolean;
}

interface Draft {
  rows: Row[];
  activeKey: number | null;
  hiddenDisplay: HiddenDisplay;
}

const NPC = "npc";
const WHOLE_NUMBER = /^-?\d+$/;

let nextKey = 0;
const newKey = () => (nextKey += 1);

function draftOf(order: TurnOrder): Draft {
  const rows = (order?.entries ?? []).map((entry) => ({
    key: newKey(),
    label: entry.label,
    characterId: entry.player_character_id ?? "",
    initiative: String(entry.initiative ?? ""),
    hidden: entry.hidden === true,
  }));
  return {
    rows,
    activeKey: rows[order?.active_index ?? -1]?.key ?? null,
    hiddenDisplay: order?.hidden_display ?? "mask",
  };
}

/** The latest unretracted initiative roll each character's player made. */
function initiativeRolls(
  events: SessionEvent[],
  members: MemberView[],
): Map<string, number> {
  const found = new Map<string, number>();
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    if (event.kind !== "roll" || event.retracted_at) continue;
    const body = event.body as { label?: unknown; total?: unknown } | null;
    if (typeof body?.label !== "string" || !/initiative/i.test(body.label))
      continue;
    const character = members.find(
      (member) => member.id === event.author_member_id,
    )?.player_character_id;
    if (character && Number.isInteger(body.total))
      found.set(character, body.total as number);
  }
  return found;
}

/** The DM sets, advances and ends the turn order. */
export function InitiativeEditor({
  campaignId,
  sessionId,
  initiative,
  characters,
  events,
  members,
}: InitiativeEditorProps) {
  const [draft, setDraft] = useState(() => draftOf(initiative));
  const [dirty, setDirty] = useState(false);
  const [ending, setEnding] = useState(false);
  // Counts edits, so a save that finishes after a newer edit leaves it unsaved.
  const revision = useRef(0);
  const toast = useToast();

  // The server's order replaces the draft only while the DM has no unsaved
  // edits. It is compared by value: a poll that changes nothing keeps the rows.
  const serverOrder = JSON.stringify(initiative ?? null);
  const shown = useRef(serverOrder);
  useEffect(() => {
    if (dirty) return;
    if (shown.current !== serverOrder)
      setDraft(draftOf(JSON.parse(serverOrder) as TurnOrder));
    shown.current = serverOrder;
  }, [serverOrder, dirty]);

  const ref = { campaignId, sessionId };
  const invalidate = [sessionKeys.session(campaignId, sessionId)];
  const save = useAction(setInitiative, {
    invalidate,
    success: "Initiative order saved.",
  });
  const advance = useAction(advanceInitiative, { invalidate });
  const end = useAction(endInitiative, {
    invalidate,
    success: "Encounter ended.",
    onSuccess: () => {
      setEnding(false);
      setDirty(false);
    },
  });
  const busy = save.isPending || advance.isPending || end.isPending;

  const { rows, activeKey, hiddenDisplay } = draft;
  const saved = (initiative?.entries?.length ?? 0) > 0;
  const invalid = rows.some(
    (row) => !row.label.trim() || !WHOLE_NUMBER.test(row.initiative.trim()),
  );
  const turnsHeld = busy || dirty || !saved;

  const edit = (change: (current: Draft) => Draft) => {
    revision.current += 1;
    setDirty(true);
    setDraft(change);
  };
  const editRows = (change: (current: Row[]) => Row[]) =>
    edit((current) => ({ ...current, rows: change(current.rows) }));
  const editRow = (key: number, change: Partial<Row>) =>
    editRows((current) =>
      current.map((row) => (row.key === key ? { ...row, ...change } : row)),
    );

  const addRow = () =>
    editRows((current) => [
      ...current,
      {
        key: newKey(),
        label: "",
        characterId: "",
        initiative: "10",
        hidden: false,
      },
    ]);
  const move = (index: number, step: number) =>
    editRows((current) => {
      const next = [...current];
      const [row] = next.splice(index, 1);
      next.splice(index + step, 0, row!);
      return next;
    });
  const pickCharacter = (row: Row, value: string) => {
    const character = characters.find((item) => item.id === value);
    // A character is never hidden, and names a row that has no name yet.
    editRow(row.key, {
      characterId: character?.id ?? "",
      hidden: character ? false : row.hidden,
      label:
        character && !row.label.trim() ? character.character_name : row.label,
    });
  };
  // Highest first. The sort is stable, so ties keep their current order.
  const sortByInitiative = () =>
    editRows((current) =>
      [...current].sort((a, b) => Number(b.initiative) - Number(a.initiative)),
    );
  const useRolls = () => {
    const rolls = initiativeRolls(events, members);
    const filled = rows.filter((row) => rolls.has(row.characterId)).length;
    if (filled === 0) {
      toast({ body: "No initiative rolls found for these characters." });
      return;
    }
    editRows((current) =>
      current.map((row) =>
        rolls.has(row.characterId)
          ? { ...row, initiative: String(rolls.get(row.characterId)) }
          : row,
      ),
    );
    toast({
      body: `Filled ${filled} initiative ${filled === 1 ? "value" : "values"} from rolls.`,
    });
  };

  const saveOrder = async () => {
    const savedRevision = revision.current;
    const result = await save.run({
      ...ref,
      order: {
        entries: rows.map((row) => ({
          label: row.label.trim(),
          player_character_id: row.characterId || null,
          initiative: Number(row.initiative),
          hidden: row.characterId ? false : row.hidden,
        })),
        hidden_display: hiddenDisplay,
        // The turn stays with its entry wherever the entry moved to.
        active_index: Math.max(
          rows.findIndex((row) => row.key === activeKey),
          0,
        ),
        round: initiative?.round || 1,
      },
    });
    if (result !== undefined && revision.current === savedRevision)
      setDirty(false);
  };

  return (
    <VStack gap={4}>
      {rows.length === 0 ? (
        <Text type="body" color="secondary">
          No one is in the order yet.
        </Text>
      ) : (
        <ol className="initiative-rows" aria-label="Initiative entries">
          {rows.map((row, index) => {
            const name = row.label.trim() || "entry";
            return (
              <li key={row.key} aria-label={row.label.trim() || "New entry"}>
                <VStack gap={3}>
                  <HStack gap={3} wrap="wrap" vAlign="end">
                    <TextInput
                      label="Name"
                      value={row.label}
                      onChange={(value) =>
                        editRow(row.key, {
                          label: value.slice(0, MAX_TURN_LABEL),
                        })
                      }
                      width={200}
                    />
                    <TextInput
                      label="Initiative"
                      value={row.initiative}
                      onChange={(value) =>
                        editRow(row.key, { initiative: value })
                      }
                      status={
                        WHOLE_NUMBER.test(row.initiative.trim())
                          ? undefined
                          : { type: "error", message: "Use a whole number." }
                      }
                      width={96}
                    />
                    <Selector
                      label="Character"
                      options={[
                        { value: NPC, label: "NPC" },
                        ...characters.map((character) => ({
                          value: character.id,
                          label: character.character_name,
                        })),
                      ]}
                      value={row.characterId || NPC}
                      onChange={(value) => pickCharacter(row, value)}
                      width={200}
                    />
                  </HStack>
                  <HStack gap={3} wrap="wrap" vAlign="center">
                    <CheckboxInput
                      label="Hidden"
                      value={row.characterId ? false : row.hidden}
                      isDisabled={Boolean(row.characterId)}
                      onChange={(hidden) => editRow(row.key, { hidden })}
                    />
                    {row.key === activeKey && (
                      <Badge variant="info" label="Active turn" />
                    )}
                  </HStack>
                  <HStack gap={2} wrap="wrap">
                    <Button
                      label={`Move ${name} up`}
                      size="sm"
                      isDisabled={index === 0}
                      onClick={() => move(index, -1)}
                    >
                      Up
                    </Button>
                    <Button
                      label={`Move ${name} down`}
                      size="sm"
                      isDisabled={index === rows.length - 1}
                      onClick={() => move(index, 1)}
                    >
                      Down
                    </Button>
                    <Button
                      label={`Remove ${name}`}
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        editRows((current) =>
                          current.filter((item) => item.key !== row.key),
                        )
                      }
                    >
                      Remove
                    </Button>
                  </HStack>
                </VStack>
              </li>
            );
          })}
        </ol>
      )}

      <RadioList
        label="Hidden entries for players"
        orientation="horizontal"
        value={hiddenDisplay}
        onChange={(value) =>
          edit((current) => ({
            ...current,
            hiddenDisplay: value as HiddenDisplay,
          }))
        }
      >
        <RadioListItem value="mask" label="Mask as ???" />
        <RadioListItem value="omit" label="Omit entirely" />
      </RadioList>

      <HStack gap={2} wrap="wrap">
        <Button
          label="Add row"
          isDisabled={rows.length >= MAX_TURN_ENTRIES}
          onClick={addRow}
        />
        <Button
          label="Sort by initiative"
          isDisabled={rows.length < 2}
          onClick={sortByInitiative}
        />
        <Button
          label="Use rolls"
          isDisabled={rows.length === 0}
          onClick={useRolls}
        />
        <Button
          label="Save order"
          variant={dirty ? "primary" : "secondary"}
          isLoading={save.isPending}
          isDisabled={busy || invalid}
          onClick={saveOrder}
        />
      </HStack>

      <HStack gap={2} wrap="wrap" role="group" aria-label="Turn controls">
        <Button
          label="Previous turn"
          isDisabled={turnsHeld}
          onClick={() => advance.run({ ...ref, direction: "previous" })}
        />
        <Button
          label="Next turn"
          variant="primary"
          isDisabled={turnsHeld}
          onClick={() => advance.run({ ...ref, direction: "next" })}
        />
        <Button
          label="End encounter"
          variant="ghost"
          isDisabled={turnsHeld}
          onClick={() => setEnding(true)}
        />
      </HStack>
      {dirty && saved && (
        <Text type="supporting">
          Save your changes to use the turn controls.
        </Text>
      )}

      <AlertDialog
        isOpen={ending}
        onOpenChange={setEnding}
        title="End this encounter?"
        description="The turn order will be cleared."
        actionLabel="End encounter"
        isActionLoading={end.isPending}
        onAction={() => end.run(ref)}
      />
    </VStack>
  );
}
