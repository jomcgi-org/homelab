import { useState } from "react";
import { useNavigate, useParams } from "react-router";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import {
  SegmentedControl,
  SegmentedControlItem,
} from "@astryxdesign/core/SegmentedControl";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { Text } from "@astryxdesign/core/Text";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { errorMessage, SignInError } from "../../api/client";
import { useCampaignJournal, type JournalView } from "../../api/journal";
import { useCharacters, useLobby } from "../../api/lobby";
import type { LobbyCampaignView } from "../../api/schema";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import { JournalPanel } from "../../journal/JournalPanel";
import { KnowledgeDrawer } from "../../knowledge/KnowledgeDrawer";
import { KnowledgeSearch } from "../../knowledge/KnowledgeSearch";

/** The viewer's journal across every session of a campaign. */
export function CampaignJournal() {
  const { campaignId = "" } = useParams();
  const lobby = useLobby();
  const campaign = lobby.data?.campaigns.find(
    (row) => row.id.toLowerCase() === campaignId.toLowerCase(),
  );
  return (
    <Page
      title={campaign ? `${campaign.name} journal` : "Journal"}
      back={{ href: paths.session(campaignId), label: "Session" }}
    >
      <Loaded query={lobby} what="your campaigns">
        {() =>
          campaign ? (
            <Sessions campaign={campaign} />
          ) : (
            <EmptyState
              title="You are not a member of this campaign"
              actions={<Button label="Your campaigns" href={paths.lobby} />}
            />
          )
        }
      </Loaded>
    </Page>
  );
}

function Sessions({ campaign }: { campaign: LobbyCampaignView }) {
  const navigate = useNavigate();
  const dm = campaign.role === "dm";
  const [view, setView] = useState<JournalView>("mine");
  const [entity, setEntity] = useState<string | null>(null);
  const journal = useCampaignJournal(campaign.id, view);
  // Only the DM's journal names other characters, by id.
  const characters = useCharacters(campaign.id, dm);
  const characterNames = Object.fromEntries(
    (characters.data ?? []).map((row) => [row.id, row.character_name]),
  );
  const sessions = journal.data?.pages.flatMap((page) => page.sessions);

  return (
    <VStack gap={6}>
      {!dm && (
        <KnowledgeSearch
          campaignId={campaign.id}
          onOpenKnowledge={setEntity}
          // The story feed lives on the session screen.
          onShowEvent={() => navigate(paths.session(campaign.id))}
        />
      )}
      <HStack>
        <SegmentedControl
          label="Journal audience"
          value={view}
          onChange={(value) => setView(value as JournalView)}
        >
          <SegmentedControlItem value="mine" label="Mine" />
          <SegmentedControlItem value="party" label="Party" />
        </SegmentedControl>
      </HStack>

      {sessions === undefined ? (
        // The journal fails closed: an error never reads as an empty journal.
        journal.isError && !(journal.error instanceof SignInError) ? (
          <EmptyState
            isCompact
            title="Could not load the journal"
            description={errorMessage(journal.error)}
            actions={
              <Button label="Try again" onClick={() => journal.refetch()} />
            }
          />
        ) : (
          <VStack gap={2} aria-busy="true">
            <Skeleton width="40%" height={20} />
            <Skeleton width="100%" height={20} />
            <Skeleton width="80%" height={20} />
          </VStack>
        )
      ) : sessions.length === 0 ? (
        <Text type="body" color="secondary">
          No sessions recorded yet.
        </Text>
      ) : (
        sessions.map((session) => (
          <VStack
            gap={4}
            as="section"
            key={session.session_id}
            aria-label={`Session ${session.started_at}`}
          >
            <Heading level={2}>
              Session{" "}
              <Timestamp
                value={session.started_at}
                format="date_time"
                type="inherit"
                color="inherit"
                hasTooltip={false}
              />
            </Heading>
            <JournalPanel
              journal={session.journal}
              view={view}
              showViewToggle={false}
              headingLevel={3}
              characterNames={characterNames}
              onOpenKnowledge={setEntity}
            />
          </VStack>
        ))
      )}
      {journal.hasNextPage && (
        <HStack>
          <Button
            label="Older sessions"
            isLoading={journal.isFetchingNextPage}
            onClick={() => journal.fetchNextPage()}
          />
        </HStack>
      )}
      {journal.isFetchNextPageError && (
        <Text type="body" color="secondary">
          Could not load older sessions. {errorMessage(journal.error)}
        </Text>
      )}
      <KnowledgeDrawer
        campaignId={campaign.id}
        entityId={entity}
        onClose={() => setEntity(null)}
      />
    </VStack>
  );
}
