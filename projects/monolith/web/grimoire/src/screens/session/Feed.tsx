import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Link } from "@astryxdesign/core/Link";
import { Text } from "@astryxdesign/core/Text";
import type { SessionEvent } from "../../api/session";
import { paths } from "../../app/paths";
import { HandoutCard } from "../../handouts/HandoutCard";
import type { InventoryPrefill } from "../../inventory/InventoryPanel";
import {
  RevealProjection,
  type RevealedKnowledge,
} from "../../knowledge/RevealProjection";
import type { ReplyTarget } from "./Composer";
import type { TableCharacter } from "./table";
import type { Table } from "./useTable";

const KIND_LABEL: Record<SessionEvent["kind"], string> = {
  narration: "The DM",
  action: "Player action",
  roll: "Dice roll",
  reveal: "New knowledge",
  handout: "Handout",
  system: "Table update",
  turn: "Turn order",
  utterance: "Spoken",
};

const AUDIENCE_LABEL: Record<SessionEvent["audience"], string> = {
  table: "Everyone",
  dm: "DM and sender",
  pcs: "Selected player and DM",
};

interface Body {
  text?: string;
  summary?: string;
  title?: string;
  name?: string;
  retracted?: boolean;
  total?: number;
  label?: string;
  formula?: string;
  rolls?: number[];
  kept?: number[];
  reveals?: RevealedKnowledge[];
  retracted_entity_ids?: string[];
  reply_to?: string;
  resolved?: boolean;
  action?: string;
  round?: number;
  active_index?: number | null;
  entries?: Array<{ label: string }>;
  entity_id?: string;
}

const bodyOf = (event: SessionEvent) => (event.body ?? {}) as Body;

export function turnSummary(body: Body): string {
  if (body.action === "end") return "The encounter has ended.";
  const entry =
    body.active_index == null ? undefined : body.entries?.[body.active_index];
  const turn = entry ? `Round ${body.round}: ${entry.label}'s turn.` : "";
  return body.action === "set"
    ? `Turn order set. ${turn}`.trim()
    : turn || "Turn order updated.";
}

/** The first line a feed entry is known by, for accessible button names. */
function subject(event: SessionEvent): string {
  const body = bodyOf(event);
  return (
    body.name ||
    body.reveals?.map((item) => item.name).join(", ") ||
    body.title ||
    body.text?.split("\n")[0] ||
    event.kind
  );
}

interface FeedProps {
  table: Table;
  playing: boolean;
  onReply: (target: ReplyTarget) => void;
  onPin: (event: SessionEvent) => void;
  onGiveItem: (prefill: InventoryPrefill) => void;
  onOpenKnowledge: (entityId: string) => void;
}

/** The story so far: every event this viewer may see, oldest first. */
export function Feed({
  table,
  playing,
  onReply,
  onPin,
  onGiveItem,
  onOpenKnowledge,
}: FeedProps) {
  const { events, session, viewer, characters, members } = table;
  const dm = viewer.dm;
  if (!session) return null;

  const authorCharacter = (event: SessionEvent): TableCharacter | undefined => {
    const member = members.find((row) => row.id === event.author_member_id);
    return characters.find((row) => row.id === member?.player_character_id);
  };
  const isResolved = (event: SessionEvent) =>
    events.some(
      (row) =>
        row.kind === "narration" &&
        !row.retracted_at &&
        bodyOf(row).reply_to === event.id &&
        bodyOf(row).resolved === true,
    );
  const give = (event: SessionEvent, knowledge?: RevealedKnowledge) =>
    onGiveItem({
      name: knowledge ? knowledge.name : subject(event).slice(0, 200),
      entity_id: knowledge?.entity_id ?? null,
      owner:
        knowledge &&
        event.audience === "pcs" &&
        event.audience_pc_ids.length === 1
          ? event.audience_pc_ids[0]!
          : "party",
      source_event_id: event.id,
    });

  if (events.length === 0) {
    const ended = session.status === "ended";
    return (
      <EmptyState
        isCompact
        headingLevel={2}
        title={ended ? "No messages were recorded." : "The story begins here."}
        description={
          ended
            ? "This session has no story entries."
            : dm
              ? session.status === "active"
                ? "Set the scene for your players."
                : "Start the session, then set the scene for your players."
              : "Take your seat. Your DM will set the scene shortly."
        }
      />
    );
  }

  return (
    <VStack gap={3} as="ol" aria-label="Session feed" className="feed">
      {events.map((event) => {
        const body = bodyOf(event);
        const gone = Boolean(event.retracted_at);
        const author = authorCharacter(event);
        const waiting =
          event.kind === "action" && event.audience === "dm" && !gone;
        return (
          <li key={event.id} id={`event-${event.id}`}>
            <Card variant={event.audience === "table" ? "default" : "muted"}>
              <VStack gap={2}>
                <HStack gap={2} wrap="wrap">
                  <Text type="label">{KIND_LABEL[event.kind]}</Text>
                  <Text type="label" color="secondary">
                    {AUDIENCE_LABEL[event.audience]}
                  </Text>
                </HStack>

                {event.kind === "roll" && !gone ? (
                  <HStack gap={3} vAlign="center">
                    <Text type="large" weight="bold" hasTabularNumbers>
                      {body.total}
                    </Text>
                    <VStack gap={0}>
                      <Text type="body">
                        {body.label || "Roll"} · {body.formula}
                      </Text>
                      <Text type="body" size="sm" color="secondary">
                        Dice: {body.rolls?.join(", ")} · Kept:{" "}
                        {body.kept?.join(", ")}
                      </Text>
                    </VStack>
                  </HStack>
                ) : event.kind === "reveal" && !gone ? (
                  (body.reveals ?? [body as RevealedKnowledge])
                    .filter(
                      (item) =>
                        !item.silent &&
                        !body.retracted_entity_ids?.includes(item.entity_id),
                    )
                    .map((knowledge) => (
                      <VStack gap={2} key={knowledge.entity_id}>
                        {knowledge.retracted ? (
                          <Text type="body" color="secondary">
                            Knowledge retracted: {knowledge.name}.
                          </Text>
                        ) : (
                          <RevealProjection knowledge={knowledge} />
                        )}
                        <HStack gap={2} wrap="wrap" vAlign="center">
                          {!knowledge.retracted &&
                            knowledge.grant_scope !== "name_only" && (
                              <>
                                <Button
                                  label={`Explore ${knowledge.name}`}
                                  size="sm"
                                  onClick={() =>
                                    onOpenKnowledge(knowledge.entity_id)
                                  }
                                />
                                <Link
                                  href={paths.entity(
                                    viewer.campaign.id,
                                    knowledge.entity_id,
                                  )}
                                  isStandalone
                                  size="sm"
                                >
                                  Open knowledge page
                                </Link>
                              </>
                            )}
                          {dm && !knowledge.retracted && (
                            <Button
                              label={`Give item ${knowledge.name}`}
                              size="sm"
                              variant="ghost"
                              onClick={() => give(event, knowledge)}
                            >
                              Give item
                            </Button>
                          )}
                        </HStack>
                      </VStack>
                    ))
                ) : event.kind === "handout" ? (
                  <HandoutCard
                    campaignId={viewer.campaign.id}
                    sessionId={session.id}
                    handout={event}
                    dm={dm}
                    onPin={onPin}
                    onOpenKnowledge={onOpenKnowledge}
                  />
                ) : event.kind === "turn" && !gone ? (
                  <Text type="body">{turnSummary(body)}</Text>
                ) : (
                  <Text type="body" color={gone ? "secondary" : "primary"}>
                    {gone
                      ? "This message was retracted."
                      : body.text || body.summary || "Session update"}
                  </Text>
                )}

                <HStack gap={2} wrap="wrap" vAlign="center">
                  {waiting && (
                    <Text type="label" color="secondary">
                      {isResolved(event) ? "Resolved" : "Waiting for DM"}
                    </Text>
                  )}
                  {waiting && dm && author && !isResolved(event) && playing && (
                    <Button
                      label={`Reply privately to ${author.character_name}`}
                      size="sm"
                      onClick={() =>
                        onReply({
                          id: event.id,
                          name: author.character_name,
                          characterId: author.id,
                        })
                      }
                    />
                  )}
                  {dm &&
                    event.kind === "handout" &&
                    !gone &&
                    !body.retracted && (
                      <Button
                        label={`Give item from ${subject(event)}`}
                        size="sm"
                        variant="ghost"
                        onClick={() => give(event)}
                      >
                        Give item
                      </Button>
                    )}
                  {!dm &&
                    !gone &&
                    !body.retracted &&
                    event.kind !== "handout" &&
                    event.kind !== "turn" && (
                      <Button
                        label={`Pin ${subject(event)} to notes`}
                        size="sm"
                        variant="ghost"
                        onClick={() => onPin(event)}
                      >
                        Pin to notes
                      </Button>
                    )}
                </HStack>
              </VStack>
            </Card>
          </li>
        );
      })}
    </VStack>
  );
}
