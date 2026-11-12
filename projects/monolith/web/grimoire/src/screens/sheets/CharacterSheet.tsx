import { useEffect, useRef } from "react";
import { Badge } from "@astryxdesign/core/Badge";
import { Blockquote } from "@astryxdesign/core/Blockquote";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, StackItem, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { useLocation } from "react-router";
import type { CharacterView } from "../../api/schema";
import {
  useSheetHistory,
  type SheetHistory,
  type SheetVersion,
} from "../../api/sheets";
import { Loaded } from "../../app/Loaded";
import { Decision } from "./Decision";
import { SheetForm } from "./SheetForm";
import { SheetSummary } from "./SheetSummary";
import { statusLabel, statusVariant } from "./status";
import { VersionHistory } from "./VersionHistory";

/**
 * One character's sheet workspace. The lobby links here by fragment, so the
 * section carries the id `character-<id>` and scrolls into view once loaded.
 */
export function CharacterSheet({ character }: { character: CharacterView }) {
  const history = useSheetHistory(character.campaign_id, character.id);
  const sectionId = `character-${character.id}`;
  const headingId = `${sectionId}-name`;
  const section = useRef<HTMLElement>(null);
  const { hash } = useLocation();
  const loaded = history.data !== undefined;
  const latest = history.data?.versions[0] ?? null;
  useEffect(() => {
    // Wait for the content: scrolling to a skeleton lands in the wrong place.
    if (loaded && hash === `#${sectionId}`) {
      section.current?.scrollIntoView?.({ block: "start" });
    }
  }, [loaded, hash, sectionId]);
  return (
    <Card>
      <VStack
        gap={4}
        as="section"
        id={sectionId}
        ref={section}
        aria-labelledby={headingId}
      >
        <HStack gap={2} vAlign="start">
          <StackItem size="fill">
            <VStack gap={1}>
              <Heading level={3} id={headingId}>
                {character.character_name}
              </Heading>
              <Text type="body" color="secondary">
                {character.class_name ?? "Unapproved class"}
                {character.level ? ` · level ${character.level}` : ""}
              </Text>
            </VStack>
          </StackItem>
          {loaded && (
            <Badge
              label={statusLabel(latest?.status)}
              variant={statusVariant(latest?.status)}
            />
          )}
        </HStack>
        <Loaded query={history} what="this sheet">
          {(data) => <Workspace history={data} />}
        </Loaded>
      </VStack>
    </Card>
  );
}

function Workspace({ history }: { history: SheetHistory }) {
  const { character, viewer_role: role, versions } = history;
  const latest = versions[0] ?? null;
  const draft = latest?.status === "draft" ? latest : null;
  const submitted = latest?.status === "submitted" ? latest : null;
  const editing = role === "player" && !submitted;
  return (
    <>
      {editing && (
        <SheetForm
          // A saved or newly opened version replaces what the form started from.
          key={latest ? `${latest.id}:${latest.updated_at}` : "new"}
          campaignId={character.campaign_id}
          characterId={character.id}
          characterName={character.character_name}
          draft={draft}
          base={latest?.sheet ?? null}
        />
      )}
      {submitted && (
        <Text type="body">
          Version {submitted.version} is waiting for the DM.
        </Text>
      )}
      {/* The editor previews the mechanics itself. */}
      {latest && !editing && <SheetSummary version={latest} />}
      {role === "dm" && submitted && (
        <Decision
          campaignId={character.campaign_id}
          characterId={character.id}
          version={submitted}
        />
      )}
      {latest?.decision_comment && <DecisionComment version={latest} />}
      <VersionHistory versions={versions} />
    </>
  );
}

function DecisionComment({ version }: { version: SheetVersion }) {
  return (
    <Blockquote
      cite={
        <>
          {version.decided_by_email}
          {version.decided_at && (
            <>
              {" · "}
              <Timestamp value={version.decided_at} format="date_time" />
            </>
          )}
        </>
      }
    >
      {version.decision_comment}
    </Blockquote>
  );
}
