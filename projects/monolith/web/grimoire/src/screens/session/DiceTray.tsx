import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import {
  SegmentedControl,
  SegmentedControlItem,
} from "@astryxdesign/core/SegmentedControl";
import { Selector } from "@astryxdesign/core/Selector";
import { TextInput } from "@astryxdesign/core/TextInput";
import { afterCommand, rollDice } from "../../api/session";
import { useAction } from "../../app/useAction";
import { sheetRolls } from "../../lib/sheet-rolls.js";
import type { Table } from "./useTable";

type Mode = "normal" | "adv" | "dis";
type Visibility = "table" | "dm" | "self";

/** A single bare d20 takes the tray's mode; any other formula is sent as typed. */
export function formulaWithMode(raw: string, mode: Mode): string {
  if (mode === "normal") return raw;
  const match = /^\s*(d20|1d20)\s*([+-]\s*\d+)?\s*$/i.exec(raw);
  if (!match) return raw;
  return `${match[1]}${mode}${match[2] ? match[2].replace(/\s+/g, "") : ""}`;
}

const SIDES = [4, 6, 8, 10, 12, 20, 100];

/** Server-rolled dice: quick dice, rolls from the approved sheet, or a formula. */
export function DiceTray({
  table,
  sessionId,
}: {
  table: Table;
  sessionId: string;
}) {
  const { viewer, characters } = table;
  const dm = viewer.dm;
  const campaignId = viewer.campaign.id;
  const sheet = dm ? null : (characters[0]?.approved ?? null);
  const [mode, setMode] = useState<Mode>("normal");
  const [kind, setKind] = useState("checks");
  const [formula, setFormula] = useState("d20");
  const [label, setLabel] = useState("");
  const [visibility, setVisibility] = useState<Visibility>(dm ? "dm" : "table");
  const roll = useAction(rollDice, {
    invalidate: afterCommand(campaignId, sessionId),
  });
  const send = (rollFormula: string, rollLabel = "") =>
    roll.run({
      campaignId,
      sessionId,
      roll: { formula: rollFormula, label: rollLabel, visibility },
    });
  const dex = sheet?.ability_modifiers?.dexterity;
  const initiative = formulaWithMode(
    `1d20${Number.isInteger(dex) && dex ? `${dex > 0 ? "+" : ""}${dex}` : ""}`,
    mode,
  );
  const presets = sheet
    ? (sheetRolls(sheet, kind, mode) as Array<{
        label: string;
        formula: string;
      }>)
    : [];
  return (
    <VStack gap={3}>
      <HStack gap={3} wrap="wrap" vAlign="end">
        <SegmentedControl
          label="Roll mode"
          value={mode}
          onChange={(value) => setMode(value as Mode)}
        >
          <SegmentedControlItem value="normal" label="Normal" />
          <SegmentedControlItem value="adv" label="Advantage" />
          <SegmentedControlItem value="dis" label="Disadvantage" />
        </SegmentedControl>
        {sheet && (
          <Selector
            label="Sheet roll type"
            options={[
              { value: "checks", label: "Ability checks" },
              { value: "saves", label: "Saving throws" },
              { value: "attacks", label: "Attacks" },
            ]}
            value={kind}
            onChange={setKind}
            width={200}
          />
        )}
      </HStack>
      {presets.length > 0 && (
        <HStack gap={2} wrap="wrap" aria-label="Approved sheet rolls">
          {presets.map((preset) => (
            <Button
              key={preset.label}
              label={preset.label}
              size="sm"
              isDisabled={roll.isPending}
              onClick={() => send(preset.formula, preset.label)}
            />
          ))}
        </HStack>
      )}
      <HStack gap={2} wrap="wrap" aria-label="Quick rolls">
        <Button
          label="Initiative"
          size="sm"
          isDisabled={roll.isPending}
          onClick={() => send(initiative, "Initiative")}
        />
        {SIDES.map((sides) => (
          <Button
            key={sides}
            label={`d${sides}`}
            size="sm"
            isDisabled={roll.isPending}
            onClick={() =>
              send(
                sides === 20 && mode !== "normal" ? `d20${mode}` : `d${sides}`,
              )
            }
          />
        ))}
      </HStack>
      <HStack gap={2} wrap="wrap" vAlign="end">
        <TextInput
          label="Dice formula"
          placeholder="d20, 2d6+3, 1d20adv"
          value={formula}
          onChange={(value) => setFormula(value.slice(0, 64))}
          onEnter={() =>
            formula.trim() && send(formulaWithMode(formula, mode), label)
          }
          width={180}
        />
        <TextInput
          label="Roll label"
          placeholder="Search for traps"
          value={label}
          onChange={(value) => setLabel(value.slice(0, 200))}
          width={200}
        />
        <Selector
          label="Roll visibility"
          options={[
            { value: "table", label: "Everyone" },
            { value: "dm", label: dm ? "DM only" : "DM and me" },
            { value: "self", label: dm ? "Just me" : "My character and DM" },
          ]}
          value={visibility}
          onChange={(value) => setVisibility(value as Visibility)}
          width={200}
        />
        <Button
          label="Roll"
          variant="primary"
          isLoading={roll.isPending}
          isDisabled={!formula.trim()}
          onClick={() => send(formulaWithMode(formula, mode), label)}
        />
      </HStack>
    </VStack>
  );
}
