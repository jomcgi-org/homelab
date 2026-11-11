import { useId, type ReactNode } from "react";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { Item } from "@astryxdesign/core/Item";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { List } from "@astryxdesign/core/List";
import {
  SegmentedControl,
  SegmentedControlItem,
} from "@astryxdesign/core/SegmentedControl";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { Text } from "@astryxdesign/core/Text";
import {
  journalSections,
  type JournalEntity,
  type JournalEvent,
  type LearnedEntry,
} from "../api/journal";
import type { EntityProjection } from "../api/knowledge";
import type { Journal } from "../api/schema";
import { HandoutText } from "../handouts/HandoutText";
import { fieldLabel, KnowledgeDetails } from "../knowledge/KnowledgeDetails";
import { projectionOf, scopeOf } from "../lib/knowledge-fields.js";
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
  /** Character names by id, so the DM reads who learned an entry. */
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
  const nothing =
    sections &&
    sections.learned.length +
      sections.received.length +
      sections.people_and_places.length +
      sections.rolls.length +
      sections.open_threads.length ===
      0;

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
          {nothing && (
            <Text type="body" color="secondary">
              Nothing recorded yet.
            </Text>
          )}
          <JournalSection title="Learned" level={headingLevel}>
            {sections.learned.map((entry) => {
              const explore = onOpenKnowledge && explorable(entry);
              const shown = source(
                entry.event_id,
                entry.name || "this discovery",
              );
              return (
                <li
                  key={`${entry.player_character_id ?? ""}:${entry.entity_id}`}
                >
                  <Card>
                    <VStack gap={2}>
                      <Learned
                        entry={entry}
                        level={entryLevel}
                        learner={
                          entry.player_character_id &&
                          characterNames[entry.player_character_id]
                        }
                      />
                      {(shown || explore) && (
                        <HStack gap={2} wrap="wrap">
                          {explore && (
                            <Button
                              label={`Explore ${entry.name || "knowledge"}`}
                              size="sm"
                              onClick={() => onOpenKnowledge(entry.entity_id)}
                            />
                          )}
                          {shown}
                        </HStack>
                      )}
                    </VStack>
                  </Card>
                </li>
              );
            })}
          </JournalSection>
          <JournalSection title="Received" level={headingLevel}>
            {sections.received.map((entry) => {
              const title = text(entry.body?.title) || "Handout";
              return (
                <li key={entry.id}>
                  <Card>
                    <VStack gap={2}>
                      <Heading level={entryLevel}>{title}</Heading>
                      <HandoutText
                        markdown={text(entry.body?.markdown)}
                        headingLevel={(entryLevel + 1) as 4 | 5 | 6}
                      />
                      {onShowEvent && (
                        <HStack>{source(entry.id, title)}</HStack>
                      )}
                    </VStack>
                  </Card>
                </li>
              );
            })}
          </JournalSection>
          <Rows title="People and places" level={headingLevel}>
            {sections.people_and_places.map((entity: JournalEntity) => (
              <Item
                key={entity.id}
                as="li"
                label={entity.name}
                description={
                  entity.entity_type
                    ? fieldLabel(entity.entity_type)
                    : undefined
                }
              />
            ))}
          </Rows>
          <Rows title="Rolls" level={headingLevel}>
            {sections.rolls.map((entry) => (
              <Roll key={entry.id} entry={entry}>
                {source(entry.id, text(entry.body?.label) || "this roll")}
              </Roll>
            ))}
          </Rows>
          <Rows title="Open threads" level={headingLevel}>
            {sections.open_threads.map((entry) => {
              const action = text(entry.body?.text).trim();
              return (
                <Item
                  key={entry.id}
                  as="li"
                  align="start"
                  label={
                    <Text type="body">
                      <span className="journal-prose">{action}</span>
                    </Text>
                  }
                  endContent={source(
                    entry.id,
                    action.split("\n")[0]!.slice(0, 80) || "this action",
                  )}
                />
              );
            })}
          </Rows>
        </>
      )}
    </VStack>
  );
}

const text = (value: unknown) => (typeof value === "string" ? value : "");

/** A name the viewer only recognises has no knowledge page to open. */
const explorable = (entry: LearnedEntry) =>
  Boolean(entry.entity_id) &&
  !entry.retracted &&
  scopeOf(entry) !== "name_only";

interface SectionProps {
  title: string;
  level: 2 | 3;
  /** One list item per entry; a section with none is not shown. */
  children: ReactNode[];
}

/** Entries that carry prose: a card each. */
function JournalSection({ title, level, children }: SectionProps) {
  const id = useId();
  if (children.length === 0) return null;
  return (
    <VStack gap={3} as="section" aria-labelledby={id}>
      <Heading level={level} id={id}>
        {title}
      </Heading>
      <VStack gap={3} as="ul" className="journal-list">
        {children}
      </VStack>
    </VStack>
  );
}

/** Entries that fit a line: divided rows. */
function Rows({ title, level, children }: SectionProps) {
  const id = useId();
  if (children.length === 0) return null;
  return (
    <VStack gap={2} as="section" aria-labelledby={id}>
      <Heading level={level} id={id}>
        {title}
      </Heading>
      <List hasDividers edge="flush">
        {children}
      </List>
    </VStack>
  );
}

/**
 * One reveal as the viewer saw it, through the renderer the feed and the
 * knowledge page use, so the journal shows exactly what the entry carries.
 */
function Learned({
  entry,
  level,
  learner,
}: {
  entry: LearnedEntry;
  level: Level;
  learner?: string;
}) {
  const recognised = scopeOf(entry) === "name_only";
  return (
    <>
      <HStack gap={2} vAlign="center" wrap="wrap">
        {entry.name && <Heading level={level}>{entry.name}</Heading>}
        {entry.entity_type && !recognised && (
          <Text type="label" color="secondary">
            {fieldLabel(entry.entity_type)}
          </Text>
        )}
      </HStack>
      {learner && (
        <Text type="body" size="sm" color="secondary">
          Shown to {learner}
        </Text>
      )}
      {entry.retracted ? (
        // Nothing of a retracted reveal is shown beyond its identity.
        <Text type="body" color="secondary">
          Retracted
        </Text>
      ) : recognised ? (
        <Text type="body">You recognize this name.</Text>
      ) : (
        <KnowledgeDetails entity={projectionOf(entry) as EntityProjection} />
      )}
    </>
  );
}

/** A roll as the feed shows it: the total, then what was rolled. */
function Roll({
  entry,
  children,
}: {
  entry: JournalEvent;
  children: ReactNode;
}) {
  const body = entry.body ?? {};
  const dice = numbers(body.rolls);
  const kept = numbers(body.kept);
  const detail = [
    dice.length > 0 && `Dice: ${dice.join(", ")}`,
    // Kept repeats the dice unless the formula dropped some.
    kept.length > 0 && kept.length < dice.length && `Kept: ${kept.join(", ")}`,
  ].filter(Boolean);
  return (
    <Item
      as="li"
      startContent={
        <Text type="large" weight="bold" hasTabularNumbers>
          {typeof body.total === "number" ? body.total : "–"}
        </Text>
      }
      // A string label is cut to one line; a long formula must wrap instead.
      label={
        <Text type="body">
          {[text(body.label) || "Roll", text(body.formula)]
            .filter(Boolean)
            .join(" · ")}
        </Text>
      }
      description={detail.length > 0 ? detail.join(" · ") : undefined}
      endContent={children}
    />
  );
}

const numbers = (value: unknown): number[] =>
  Array.isArray(value)
    ? value.filter((item): item is number => typeof item === "number")
    : [];
