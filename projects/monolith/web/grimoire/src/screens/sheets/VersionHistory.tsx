import { Collapsible } from "@astryxdesign/core/Collapsible";
import { VStack } from "@astryxdesign/core/Layout";
import { List, ListItem } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import type { SheetVersion } from "../../api/sheets";
import { statusLabel } from "./status";

/** Every version of a sheet, newest first. Closed until asked for. */
export function VersionHistory({ versions }: { versions: SheetVersion[] }) {
  return (
    <Collapsible
      defaultIsOpen={false}
      trigger={<Text type="label">Version history ({versions.length})</Text>}
    >
      <VStack paddingBlockStart={2}>
        {versions.length === 0 ? (
          <Text type="body" color="secondary">
            No versions submitted.
          </Text>
        ) : (
          <List hasDividers>
            {versions.map((version) => (
              <ListItem
                key={version.id}
                label={`v${version.version} · ${statusLabel(version.status)}`}
                endContent={
                  <Timestamp value={version.created_at} format="date_time" />
                }
              />
            ))}
          </List>
        )}
      </VStack>
    </Collapsible>
  );
}
