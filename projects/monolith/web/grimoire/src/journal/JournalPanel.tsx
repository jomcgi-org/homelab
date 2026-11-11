import { useId, type ReactNode } from "react";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { Item } from "@astryxdesign/core/Item";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { List } from "@astryxdesign/core/List";
import {
  MetadataList,
  MetadataListItem,
} from "@astryxdesign/core/MetadataList";
import {
  SegmentedControl,
  SegmentedControlItem,
} from "@astryxdesign/core/SegmentedControl";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { Text } from "@astryxdesign/core/Text";
import {
  journalSections,
  type JournalEvent,
  type LearnedEntry,
} from "../api/journal";
import type { Journal } from "../api/schema";
import { HandoutText } from "../handouts/HandoutText";
import "./journal.css";

export type JournalView = "mine" | "party";

export interface JournalPanelProps {
  /** Null while loading or when the session has no journal yet. */
  journal: Journal | null;
  view: JournalView;
  /** Omit together with `showViewToggle={false}` when the page owns the view. */
  onViewChange?: (view: JournalView) => void;
  /** Omit where there is no story feed to show the event in. */
  onShowEvent?: (eventId: string) => void;
  onOpenKnowledge?: (entityId: string) => void;
  showViewToggle?: boolean;
  /** The level of the section headings; entries sit one below. */
  headingLevel?: 2 | 3;
  /** Character names by id, so the DM reads a name instead of an id. */
  characterNames?: Record<string, string>;
}

type Level = 2 | 3 | 4 | 5;

/** What this viewer learned, received and rolled in the session. */
export function JournalPanel({
  journal,
  view,
  onViewChange,
  onShowEvent,
  onOpenKnowledge,
  showViewToggle = true,
  headingLevel = 2,
  characterNames = {},
}: JournalPanelProps) {
  const sections = journal && journalSections(journal);
  const entryLevel = (headingLevel + 1) as Level;
  // A truncated journal folds only the earliest events, so an empty section
  // is "nothing yet in the part shown", never a claim about the whole session.
  const empty = (text: string) =>
    sections?.truncated
      ? `${text.replace(/\.$/, "")} in the events shown.`
      : text;
  const source = (eventId: string | undefined, subject: string) =>
    onShowEvent && eventId ? (
      <Button
        label={`Show ${subject} in story`}
        size="sm"
        variant="ghost"
        onClick={() => onShowEvent(eventId)}
      >
        Show in story
      </Button>
    ) : null;
  const events = (entries: JournalEvent[]) =>
    entries.map((entry) => (
      <Card as="li" key={entry.id}>
        <VStack gap={2}>
          <EventBody entry={entry} level={entryLevel} />
          {source(entry.id, eventSubject(entry))}
        </VStack>
      </Card>
    ));

  return (
    <VStack gap={5} as="section" aria-label="Session journal">
      {showViewToggle && onViewChange && (
        <HStack>
          <SegmentedControl
            label="Journal audience"
            value={view}
            onChange={(value) => onViewChange(value as JournalView)}
          >
            <SegmentedControlItem value="mine" label="Mine" />
            <SegmentedControlItem value="party" label="Party" />
          </SegmentedControl>
        </HStack>
      )}
      {!sections ? (
        <VStack gap={2} aria-busy="true">
          <Skeleton width="40%" height={20} />
          <Skeleton width="100%" height={20} />
          <Skeleton width="80%" height={20} />
        </VStack>
      ) : (
        <>
          {sections.truncated && (
            <Banner
              status="warning"
              title="This journal is incomplete"
              description="The session has more events than can be shown, so later handouts, rolls and replies are missing. Open threads may already have been answered."
            />
          )}
          <JournalSection
            title="Learned"
            level={headingLevel}
            empty={empty("No discoveries yet.")}
          >
            {sections.learned.map((entry) => (
              <Card
                as="li"
                key={`${entry.player_character_id ?? ""}:${entry.entity_id}`}
              >
                <VStack gap={2}>
                  <Learned
                    entry={entry}
                    level={entryLevel}
                    characterNames={characterNames}
                  />
                  {((onShowEvent && entry.event_id) ||
                    (onOpenKnowledge && explorable(entry))) && (
                    <HStack gap={2} wrap="wrap">
                      {source(entry.event_id, entry.name || "this discovery")}
                      {onOpenKnowledge && explorable(entry) && (
                        <Button
                          label={`Explore ${entry.name || "knowledge"}`}
                          size="sm"
                          onClick={() => onOpenKnowledge(entry.entity_id)}
                        />
                      )}
                    </HStack>
                  )}
                </VStack>
              </Card>
            ))}
          </JournalSection>
          <JournalSection
            title="Received"
            level={headingLevel}
            empty={empty("No handouts received yet.")}
          >
            {events(sections.received)}
          </JournalSection>
          <PeopleAndPlaces
            people={sections.people_and_places}
            level={headingLevel}
            empty={empty("No people or places recorded yet.")}
          />
          <JournalSection
            title="Rolls"
            level={headingLevel}
            empty={empty("No rolls recorded yet.")}
          >
            {events(sections.rolls)}
          </JournalSection>
          <JournalSection
            title="Open threads"
            level={headingLevel}
            empty={empty("No open threads.")}
          >
            {events(sections.open_threads)}
          </JournalSection>
        </>
      )}
    </VStack>
  );
}

/** A name the viewer only recognises has no knowledge page to open. */
const explorable = (entry: LearnedEntry) =>
  Boolean(entry.entity_id) &&
  !entry.retracted &&
  entry.grant_scope !== "name_only" &&
  !entry.entity?.recognition_only;

/** What an event is known by, for the accessible name of its button. */
function eventSubject(entry: JournalEvent): string {
  const body = entry.body ?? {};
  for (const key of ["title", "label", "text"]) {
    const value = body[key];
    if (typeof value === "string" && value.trim())
      return value.trim().split("\n")[0]!.slice(0, 80);
  }
  return entry.kind === "roll" ? "this roll" : "this entry";
}

interface SectionProps {
  title: string;
  level: 2 | 3;
  empty: string;
  /** One list item per entry. */
  children: ReactNode[];
}

function JournalSection({ title, level, empty, children }: SectionProps) {
  const id = useId();
  return (
    <VStack gap={3} as="section" aria-labelledby={id}>
      <Heading level={level} id={id}>
        {title}
      </Heading>
      {children.length === 0 ? (
        <Text type="body" color="secondary">
          {empty}
        </Text>
      ) : (
        <VStack gap={3} as="ul" className="journal-list">
          {children}
        </VStack>
      )}
    </VStack>
  );
}

function PeopleAndPlaces({
  people,
  level,
  empty,
}: {
  people: Array<{ id: string; name: string; entity_type?: string | null }>;
  level: 2 | 3;
  empty: string;
}) {
  const id = useId();
  return (
    <VStack gap={3} as="section" aria-labelledby={id}>
      <Heading level={level} id={id}>
        People and places
      </Heading>
      {people.length === 0 ? (
        <Text type="body" color="secondary">
          {empty}
        </Text>
      ) : (
        <List hasDividers>
          {people.map((entity) => (
            <Item
              key={entity.id}
              as="li"
              label={entity.name}
              description={entity.entity_type ?? undefined}
            />
          ))}
        </List>
      )}
    </VStack>
  );
}

function Learned({
  entry,
  level,
  characterNames,
}: {
  entry: LearnedEntry;
  level: Level;
  characterNames: Record<string, string>;
}) {
  const character = entry.player_character_id;
  return (
    <>
      {entry.name && <Heading level={level}>{entry.name}</Heading>}
      {entry.entity_type && (
        <Text type="label" color="secondary">
          {entry.entity_type}
        </Text>
      )}
      {entry.retracted ? (
        // Nothing of a retracted reveal is shown beyond its identity.
        <Text type="body" weight="semibold">
          Retracted
        </Text>
      ) : (
        <>
          {(entry.grant_scope === "name_only" ||
            entry.entity?.recognition_only) && (
            <Text type="body">You recognize this name.</Text>
          )}
          {entry.grant_scope && (
            <Text type="body">Scope: {entry.grant_scope}</Text>
          )}
          {character && (
            <Text type="body">
              Character: {characterNames[character] ?? character}
            </Text>
          )}
          {entry.entity && <Fields value={entry.entity} />}
        </>
      )}
    </>
  );
}

function EventBody({ entry, level }: { entry: JournalEvent; level: Level }) {
  const body = entry.body ?? {};
  if (entry.kind !== "handout") return <Fields value={body} />;
  const title = typeof body.title === "string" && body.title;
  const markdown = typeof body.markdown === "string" ? body.markdown : "";
  return (
    <>
      <Heading level={level}>{title || "Handout"}</Heading>
      <HandoutText
        markdown={markdown}
        headingLevel={(level + 1) as 4 | 5 | 6}
      />
    </>
  );
}

/** A record shown field by field; every value renders as text. */
function Fields({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value);
  if (entries.length === 0) return null;
  return (
    <MetadataList>
      {entries.map(([key, detail]) => (
        <MetadataListItem key={key} label={key.replaceAll("_", " ")}>
          {typeof detail === "object" && detail !== null ? (
            <pre className="journal-json">
              {JSON.stringify(detail, null, 2)}
            </pre>
          ) : (
            String(detail ?? "")
          )}
        </MetadataListItem>
      ))}
    </MetadataList>
  );
}
