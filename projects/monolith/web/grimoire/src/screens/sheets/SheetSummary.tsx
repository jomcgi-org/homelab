import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import {
  MetadataList,
  MetadataListItem,
} from "@astryxdesign/core/MetadataList";
import { ABILITIES, type SheetVersion } from "../../api/sheets";

const signed = (value: number) => (value >= 0 ? `+${value}` : String(value));

/** What a version says: its base facts and the mechanics calculated from them. */
export function SheetSummary({ version }: { version: SheetVersion }) {
  const { sheet, derived } = version;
  return (
    <>
      <VStack gap={2}>
        <Heading level={4}>Version {version.version}</Heading>
        <MetadataList columns={3}>
          <MetadataListItem label="Ancestry">{sheet.ancestry}</MetadataListItem>
          <MetadataListItem label="Class">{sheet.class_name}</MetadataListItem>
          <MetadataListItem label="Level">{sheet.level}</MetadataListItem>
          {ABILITIES.map(({ name, label }) => (
            <MetadataListItem key={name} label={label}>
              {sheet.ability_scores[name]}
            </MetadataListItem>
          ))}
        </MetadataList>
      </VStack>
      {derived && (
        <VStack gap={2}>
          <Heading level={4}>Calculated mechanics</Heading>
          <MetadataList columns={2}>
            <MetadataListItem label="Proficiency">
              {signed(derived.proficiency_bonus)}
            </MetadataListItem>
            <MetadataListItem label="Unarmored AC">
              {derived.unarmored_armor_class}
            </MetadataListItem>
            <MetadataListItem label="Max HP">
              {derived.max_hit_points}
            </MetadataListItem>
            <MetadataListItem label="Hit die">
              {derived.hit_die}
            </MetadataListItem>
            {ABILITIES.map(({ name, label }) => (
              <MetadataListItem key={name} label={`${label} mod / save`}>
                {signed(derived.ability_modifiers[name])} /{" "}
                {signed(derived.saving_throw_bonuses[name])}
              </MetadataListItem>
            ))}
          </MetadataList>
        </VStack>
      )}
    </>
  );
}
