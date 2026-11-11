import { useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Button } from "@astryxdesign/core/Button";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Link } from "@astryxdesign/core/Link";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { errorMessage } from "../../api/client";
import { pinEvent } from "../../api/notes";
import {
  afterCommand,
  setSessionStatus,
  startSession,
  useJournal,
  type SessionEvent,
  type TurnOrder,
} from "../../api/session";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import { useAction } from "../../app/useAction";
import { useSignIn } from "../../app/useSignIn";
import { HandoutComposer } from "../../handouts/HandoutComposer";
import {
  InventoryPanel,
  type InventoryPrefill,
} from "../../inventory/InventoryPanel";
import { JournalPanel, type JournalView } from "../../journal/JournalPanel";
import { KnowledgeDrawer } from "../../knowledge/KnowledgeDrawer";
import { KnowledgeSearch } from "../../knowledge/KnowledgeSearch";
import { RevealPanel } from "../../knowledge/RevealPanel";
import { dropDraft } from "../../lib/session-expiry.js";
import { SessionNotesPanel } from "../../notes/SessionNotesPanel";
import { InitiativeEditor } from "../../turns/InitiativeEditor";
import { TurnStrip } from "../../turns/TurnStrip";
import { YourTurnBanner } from "../../turns/YourTurnBanner";
import { ReadAloud } from "../../voices/ReadAloud";
import { VoicePresets } from "../../voices/VoicePresets";
import { Composer, type ReplyTarget } from "./Composer";
import { DiceTray } from "./DiceTray";
import { Feed } from "./Feed";
import "./session.css";
import { useTable, type Table } from "./useTable";

type TabName = "story" | "notes" | "inventory" | "journal";

/** The live table for one campaign: the story feed and everything around it. */
export function Session() {
  const { campaignId = "" } = useParams();
  const [search] = useSearchParams();
  const state = useTable(campaignId, search.get("session"));
  const back = { href: paths.lobby, label: "Your campaigns" };

  if (state.status === "ready") return <TableView table={state.table} />;
  return (
    <Page title="Session" back={back} width={1100}>
      {state.status === "loading" ? (
        <VStack gap={2} aria-busy="true">
          <Skeleton width="40%" height={24} />
          <Skeleton width="100%" height={120} />
        </VStack>
      ) : state.status === "error" ? (
        <EmptyState
          title="Could not open the session"
          description={errorMessage(state.error)}
          actions={<Button label="Try again" onClick={state.retry} />}
        />
      ) : (
        <EmptyState
          title={
            state.status === "not-member"
              ? "You are not a member of this campaign"
              : "Session not found"
          }
          actions={<Button label="Your campaigns" href={paths.lobby} />}
        />
      )}
    </Page>
  );
}

/** A player without the DM's order can still follow the latest turn event. */
function turnOrder(table: Table): TurnOrder {
  if (table.initiative?.entries?.length) return table.initiative;
  if (table.viewer.dm) return table.initiative;
  const last = table.events
    .filter((event) => event.kind === "turn" && !event.retracted_at)
    .at(-1);
  return (last?.body as TurnOrder | undefined) ?? null;
}

function TableView({ table }: { table: Table }) {
  const { viewer, session, characters } = table;
  const campaignId = viewer.campaign.id;
  const dm = viewer.dm;
  const playing = session !== null && session.status !== "ended";
  const [tab, setTab] = useState<TabName>("story");
  const [entity, setEntity] = useState<string | null>(null);
  const [journalView, setJournalView] = useState<JournalView>("mine");
  const [prefill, setPrefill] = useState<InventoryPrefill | null>(null);
  const [reply, setReply] = useState<ReplyTarget | null>(null);
  const [ending, setEnding] = useState(false);
  const journal = useJournal(
    campaignId,
    session,
    journalView,
    tab === "journal",
  );
  const signInState = useSignIn();
  const connection =
    signInState === "signed-out"
      ? "Signed out"
      : signInState === "reconnecting" || !table.feedLive
        ? "Reconnecting"
        : "Live";

  // Another account signing in on this browser changes who the polls answer
  // for. Panels hold their own state, so only a full load is safe. A draft
  // kept for the earlier account is dropped, never shown to the new one.
  const identity = `${viewer.user.id}|${viewer.campaign.role}|${viewer.campaign.player_character_id}`;
  const firstIdentity = useRef(identity);
  const firstUser = useRef(viewer.user.id);
  useEffect(() => {
    if (identity === firstIdentity.current) return;
    if (viewer.user.id !== firstUser.current) dropDraft(campaignId);
    location.reload();
  }, [identity, viewer.user.id, campaignId]);

  const invalidate = afterCommand(campaignId, session?.id);
  const start = useAction(startSession, { invalidate });
  const setStatus = useAction(setSessionStatus, {
    invalidate,
    onSuccess: () => setEnding(false),
  });
  const pin = useAction(pinEvent, {
    invalidate: [["campaign", campaignId, "notes"]],
    success: "Pinned to your notes.",
    onSuccess: () => setTab("notes"),
  });

  const showEvent = (eventId: string) => {
    setTab("story");
    requestAnimationFrame(() =>
      document
        .getElementById(`event-${eventId}`)
        ?.scrollIntoView({ block: "center" }),
    );
  };
  const order = turnOrder(table);
  const ownCharacterIds = dm ? [] : characters.map((row) => row.id);

  return (
    <Page
      title={viewer.campaign.name}
      eyebrow={
        dm ? "Dungeon master" : (characters[0]?.character_name ?? "Player")
      }
      back={{ href: paths.lobby, label: "Your campaigns" }}
      width={1100}
      actions={
        <HStack gap={3} vAlign="center">
          <Text type="label" color="secondary">
            {session ? session.status : "Not started"}
          </Text>
          <StatusDot
            variant={connection === "Live" ? "success" : "warning"}
            label={`Session connection: ${connection}`}
          />
          <Text type="label">{connection}</Text>
        </HStack>
      }
    >
      <ReadAloud
        key={`${campaignId}:${session?.id}`}
        campaignId={campaignId}
        role={viewer.campaign.role}
        characterId={viewer.campaign.player_character_id}
        events={table.events}
        voices={table.voices}
      />
      {!dm && (
        <KnowledgeSearch
          campaignId={campaignId}
          onOpenKnowledge={setEntity}
          onShowEvent={showEvent}
        />
      )}
      <TabList
        label="Session sections"
        value={tab}
        onChange={(value) => setTab(value as TabName)}
      >
        <Tab value="story" label="Story" />
        <Tab value="notes" label="Notes" />
        <Tab value="inventory" label="Inventory" />
        <Tab value="journal" label="Journal" />
      </TabList>

      {tab === "inventory" && (
        <InventoryPanel
          campaignId={campaignId}
          dm={dm}
          characters={characters}
          prefill={prefill}
          onOpenKnowledge={setEntity}
        />
      )}
      {/* Notes stay mounted so an unsaved note survives a tab switch. */}
      <div hidden={tab !== "notes"}>
        <SessionNotesPanel
          campaignId={campaignId}
          dm={dm}
          onShowEvent={showEvent}
          onOpenKnowledge={setEntity}
        />
      </div>
      {tab === "journal" &&
        (session ? (
          <JournalPanel
            journal={journal.data ?? null}
            view={journalView}
            onViewChange={setJournalView}
            onShowEvent={showEvent}
            onOpenKnowledge={setEntity}
          />
        ) : (
          <Text type="body" color="secondary">
            Your journal starts when the session does.
          </Text>
        ))}

      <div className="table-layout" hidden={tab !== "story"}>
        <VStack gap={4}>
          {!dm && (
            <YourTurnBanner order={order} characterIds={ownCharacterIds} />
          )}
          <TurnStrip order={order} dm={dm} />
          {dm && playing && session && (
            <Collapsible
              defaultIsOpen={false}
              trigger={<Text type="label">Initiative order</Text>}
            >
              <InitiativeEditor
                campaignId={campaignId}
                sessionId={session.id}
                initiative={table.initiative}
                characters={characters}
                events={table.events}
                members={table.members}
              />
            </Collapsible>
          )}
          <Feed
            table={table}
            playing={playing}
            onReply={setReply}
            onPin={(event: SessionEvent) =>
              pin.run({ campaignId, eventId: event.id })
            }
            onGiveItem={(next) => {
              setPrefill(next);
              setTab("inventory");
            }}
            onOpenKnowledge={setEntity}
          />
          {playing && session ? (
            <>
              <Composer
                table={table}
                sessionId={session.id}
                reply={reply}
                onReplyDone={() => setReply(null)}
              />
              {dm && (
                <VoicePresets campaignId={campaignId} presets={table.voices} />
              )}
              {dm && (
                <Collapsible
                  defaultIsOpen={false}
                  trigger={<Text type="label">Send a handout</Text>}
                >
                  <HandoutComposer
                    campaignId={campaignId}
                    sessionId={session.id}
                    characters={characters}
                  />
                </Collapsible>
              )}
              <Collapsible
                defaultIsOpen={false}
                trigger={<Text type="label">Roll dice</Text>}
              >
                <DiceTray table={table} sessionId={session.id} />
              </Collapsible>
            </>
          ) : (
            session && (
              <Text type="body" color="secondary">
                This session has ended. Your story is saved here.
              </Text>
            )
          )}
        </VStack>

        <VStack
          gap={4}
          as="aside"
          aria-label={dm ? "At your table" : "Your character"}
        >
          {dm && playing && (
            <RevealPanel campaignId={campaignId} characters={characters} />
          )}
          <Heading level={2}>{dm ? "At your table" : "Your character"}</Heading>
          {characters.map((character) => (
            <VStack gap={0} key={character.id}>
              <Text type="body" weight="semibold">
                {character.character_name}
              </Text>
              <Text type="body" size="sm" color="secondary">
                Level {character.level || 1}{" "}
                {character.class_name || "adventurer"}
                {character.approved &&
                  ` · HP ${character.approved.max_hit_points} · AC ${character.approved.unarmored_armor_class}`}
              </Text>
            </VStack>
          ))}
          <Link href={paths.sheets()} isStandalone>
            Open character sheets
          </Link>
          {dm && (
            <Link href={paths.grants(campaignId)} isStandalone>
              Manage knowledge grants
            </Link>
          )}
          {dm && (
            <HStack gap={2} wrap="wrap">
              {!playing ? (
                <Button
                  label="Start session"
                  variant="primary"
                  isLoading={start.isPending}
                  onClick={() => start.run(campaignId)}
                />
              ) : (
                <>
                  <Button
                    label={
                      session!.status === "paused"
                        ? "Resume session"
                        : "Pause session"
                    }
                    isDisabled={setStatus.isPending}
                    onClick={() =>
                      setStatus.run({
                        campaignId,
                        sessionId: session!.id,
                        status:
                          session!.status === "paused" ? "active" : "paused",
                      })
                    }
                  />
                  <Button
                    label="End session"
                    variant="ghost"
                    onClick={() => setEnding(true)}
                  />
                </>
              )}
            </HStack>
          )}
        </VStack>
      </div>

      <KnowledgeDrawer
        campaignId={campaignId}
        entityId={entity}
        onClose={() => setEntity(null)}
      />
      <AlertDialog
        isOpen={ending}
        onOpenChange={setEnding}
        title="End this session?"
        description="The story stays available to everyone at the table. Nobody can add to it afterwards."
        actionLabel="End session"
        isActionLoading={setStatus.isPending}
        onAction={() =>
          session &&
          setStatus.run({ campaignId, sessionId: session.id, status: "ended" })
        }
      />
    </Page>
  );
}
