import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { useAnnounce } from "@astryxdesign/core/hooks";
import { Item } from "@astryxdesign/core/Item";
import { HStack, StackItem, VStack } from "@astryxdesign/core/Layout";
import { Link } from "@astryxdesign/core/Link";
import { List } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { errorMessage } from "../api/client";
import {
  disputeFact,
  knowledgeKeys,
  searchKnowledge,
  type EntityHit,
  type FactHit,
  type KnowledgeHit,
} from "../api/knowledge";
import { paths } from "../app/paths";
import { useAction } from "../app/useAction";
import { fieldLabel } from "./KnowledgeDetails";

export interface KnowledgeSearchProps {
  campaignId: string;
  /** Open an entity in the knowledge drawer. */
  onOpenKnowledge: (entityId: string) => void;
  /** Scroll the feed to a session event. */
  onShowEvent: (eventId: string) => void;
}

const MAX_QUERY = 200;
const INVALID = `Enter a search of 1 to ${MAX_QUERY} characters.`;

const BADGE: Record<KnowledgeHit["type"], string> = {
  entity: "Entity",
  note: "Note",
  event: "Event",
  chunk: "Lore",
  fact: "Fact",
};

/** The lore reader is public and lives on the main site, outside this app. */
const loreUrl = (bookId: string, chunkId: string) =>
  `https://jomcgi.dev/app/grimoire/book/${encodeURIComponent(bookId)}/c/${encodeURIComponent(chunkId)}`;

/**
 * An entity hit is a grant projection with no preview text. Only its revealed
 * details or its type are shown; the entity itself is never fetched here.
 */
function entityPreview(hit: EntityHit): string {
  if (!hit.revealed_details) return fieldLabel(hit.entity_type ?? "");
  return Object.entries(hit.revealed_details)
    .map(
      ([key, value]) =>
        `${fieldLabel(key)}: ${typeof value === "string" ? value : JSON.stringify(value)}`,
    )
    .join(" · ");
}

/** Player search across what their character knows. */
export function KnowledgeSearch(props: KnowledgeSearchProps) {
  // A different campaign starts empty: results are private to the one searched.
  return <Search key={props.campaignId} {...props} />;
}

function Search({
  campaignId,
  onOpenKnowledge,
  onShowEvent,
}: KnowledgeSearchProps) {
  const [text, setText] = useState("");
  const [invalid, setInvalid] = useState(false);
  // Each submission is its own query, so searching again clears the previous
  // results at once and a failure leaves none of them behind.
  const [submitted, setSubmitted] = useState<{ q: string; attempt: number }>();
  const [disputed, setDisputed] = useState<string[]>([]);
  const announce = useAnnounce({ hasReplace: true });
  const search = useQuery({
    queryKey: knowledgeKeys.search(
      campaignId,
      submitted?.q ?? "",
      submitted?.attempt ?? 0,
    ),
    enabled: submitted !== undefined,
    queryFn: ({ signal }) =>
      searchKnowledge({ campaignId, q: submitted!.q, signal }),
    retry: false,
    staleTime: Infinity,
    gcTime: 0,
  });
  const dispute = useAction(disputeFact, {
    onSuccess: (_result, input) => setDisputed((ids) => [...ids, input.factId]),
  });

  const hits = search.data;
  const count =
    hits && `${hits.length} ${hits.length === 1 ? "result" : "results"} found.`;
  useEffect(() => {
    if (count) announce(count);
  }, [announce, count]);

  const submit = () => {
    const q = text.trim();
    const valid = q.length >= 1 && q.length <= MAX_QUERY;
    setInvalid(!valid);
    setDisputed([]);
    setSubmitted(
      valid ? { q, attempt: (submitted?.attempt ?? 0) + 1 } : undefined,
    );
  };

  return (
    <VStack gap={3} as="section" aria-label="Character knowledge search">
      <HStack gap={2} vAlign="end">
        <StackItem size="fill">
          <TextInput
            type="search"
            label="Search what your character knows"
            value={text}
            onChange={setText}
            onEnter={submit}
            enterKeyHint="search"
            hasClear
            width="100%"
            status={
              invalid
                ? { type: "error", message: INVALID }
                : search.isError
                  ? { type: "error", message: errorMessage(search.error) }
                  : undefined
            }
            statusVariant="detached"
          />
        </StackItem>
        <StackItem size="static">
          <Button
            label="Search knowledge"
            isLoading={search.isFetching}
            onClick={submit}
          />
        </StackItem>
      </HStack>
      {count && (
        <Text type="body" color="secondary">
          {hits.length ? count : "No knowledge matches your search."}
        </Text>
      )}
      {hits && hits.length > 0 && (
        <List hasDividers header="Knowledge results">
          {hits.map((hit) => (
            <Item
              key={`${hit.type}:${hit.id}`}
              as="li"
              align="start"
              startContent={<Badge label={BADGE[hit.type]} />}
              label={
                hit.type === "fact" ? (
                  <FactHeading hit={hit} disputed={disputed.includes(hit.id)} />
                ) : hit.type === "entity" ? (
                  <Button
                    label={`Explore ${hit.name}`}
                    size="sm"
                    onClick={() => onOpenKnowledge(hit.source.entity_id)}
                  />
                ) : hit.type === "note" ? (
                  <Link
                    href={`${paths.notes(campaignId)}?note=${encodeURIComponent(hit.source.note_id)}#note-${encodeURIComponent(hit.source.note_id)}`}
                  >
                    {hit.title || "Untitled note"}
                  </Link>
                ) : hit.type === "event" ? (
                  <Link
                    href={`${paths.session(campaignId)}?session=${encodeURIComponent(hit.source.session_id)}`}
                    onClick={() => onShowEvent(hit.id)}
                  >
                    Session event {hit.source.seq}
                  </Link>
                ) : (
                  <Link
                    href={loreUrl(hit.source.book_id, hit.source.chunk_id)}
                    isExternalLink
                  >
                    {hit.display_name}
                  </Link>
                )
              }
              description={
                hit.type === "entity" ? entityPreview(hit) : (hit.preview ?? "")
              }
              endContent={
                hit.type === "fact" &&
                hit.can_dispute &&
                hit.status !== "disputed" &&
                !disputed.includes(hit.id) && (
                  <Button
                    label="Dispute fact"
                    size="sm"
                    variant="ghost"
                    isDisabled={dispute.isPending}
                    onClick={() => dispute.run({ campaignId, factId: hit.id })}
                  />
                )
              }
            />
          ))}
        </List>
      )}
    </VStack>
  );
}

function FactHeading({ hit, disputed }: { hit: FactHit; disputed: boolean }) {
  const isDisputed = disputed || hit.status === "disputed";
  return (
    <Text type="body">
      {hit.ownership === "party" ? "Party fact" : "Character fact"} ·{" "}
      {!isDisputed
        ? "Active"
        : hit.can_dispute
          ? "Disputed. Excluded from campaign context."
          : "Disputed"}
    </Text>
  );
}
