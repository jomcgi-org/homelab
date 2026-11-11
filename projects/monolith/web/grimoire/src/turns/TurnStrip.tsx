import { Badge } from "@astryxdesign/core/Badge";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import type { TurnOrder } from "../api/session";
import "./turns.css";

export interface TurnStripProps {
  order: TurnOrder;
  dm: boolean;
}

/** What players see in place of a hidden entry's name. */
const MASKED = "???";

/** The turn order at a glance: round, entries and whose turn it is. */
export function TurnStrip({ order, dm }: TurnStripProps) {
  const entries = order?.entries ?? [];
  if (entries.length === 0) return null;
  const activeIndex = order?.active_index ?? null;
  return (
    <VStack gap={2} as="section" aria-label="Turn order">
      <Text type="label" color="secondary">
        Round {order?.round ?? 1}
      </Text>
      <ol className="turn-order">
        {entries.map((entry, index) => {
          const active = index === activeIndex;
          // Players already receive the server's projection. A hidden entry is
          // masked again here so a secret name cannot reach a player's page
          // even if a caller passes the DM's view.
          const concealed = entry.hidden === true && !dm;
          return (
            <li key={index} aria-current={active ? "step" : undefined}>
              <Text type="body" weight={active ? "semibold" : undefined}>
                {concealed ? MASKED : entry.label}
              </Text>
              {entry.hidden === true && dm && <Badge label="hidden" />}
              {active && <Badge variant="info" label="Active turn" />}
            </li>
          );
        })}
      </ol>
    </VStack>
  );
}
