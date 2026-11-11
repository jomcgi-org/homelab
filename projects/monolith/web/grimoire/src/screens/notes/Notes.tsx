import { useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { HStack, StackItem, VStack } from "@astryxdesign/core/Layout";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useLobby } from "../../api/lobby";
import { useNote, useNotes, type NoteKind } from "../../api/notes";
import type { LobbyCampaignView } from "../../api/schema";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import "../../notes/notes.css";
import { AddNote } from "./AddNote";
import { NoteCard } from "./NoteCard";

const SEARCH_MAX = 20_000;

/** Campaign notes outside a session: character and party notes. */
export function Notes() {
  const { campaignId = "" } = useParams();
  const lobby = useLobby();
  const campaign = lobby.data?.campaigns.find(
    (row) => row.id.toLowerCase() === campaignId.toLowerCase(),
  );
  return (
    <Page
      title={campaign ? `${campaign.name} notes` : "Notes"}
      back={{ href: paths.session(campaignId), label: "Back to session" }}
    >
      <Loaded query={lobby} what="this campaign">
        {() =>
          campaign ? (
            <CampaignNotes key={campaign.id} campaign={campaign} />
          ) : (
            <EmptyState
              title="Campaign not found"
              actions={<Button label="Your campaigns" href={paths.lobby} />}
            />
          )
        }
      </Loaded>
    </Page>
  );
}

const emptyText = (kind: NoteKind, dm: boolean, searching: boolean) =>
  `No ${
    kind === "party"
      ? "party notes"
      : dm
        ? "notes shared with you"
        : "personal notes"
  }${searching ? " match your search" : " yet"}.`;

function CampaignNotes({ campaign }: { campaign: LobbyCampaignView }) {
  const [search, setSearch] = useSearchParams();
  const dm = campaign.role === "dm";
  const q = search.get("q") ?? "";
  const [typed, setTyped] = useState(q);

  // A link to one note opens the tab that note lives on.
  const linked = useNote(campaign.id, search.get("note"));
  const kind: NoteKind =
    linked.data?.kind ??
    (search.get("kind") === "party" ? "party" : "character");
  const notes = useNotes(campaign.id, kind, { q });

  const show = (nextKind: NoteKind, nextQ: string) =>
    setSearch({ kind: nextKind, ...(nextQ ? { q: nextQ } : {}) });
  // Players write once they hold a character; a DM writes party notes only.
  const canAdd =
    (dm || Boolean(campaign.player_character_id)) && (!dm || kind === "party");

  if (search.has("note") && linked.data === undefined) {
    return (
      <Loaded query={linked} what="that note">
        {() => null}
      </Loaded>
    );
  }
  return (
    <VStack gap={5} as="section" aria-label="Campaign notes">
      <TabList
        aria-label="Note audience"
        value={kind}
        onChange={(value) => show(value as NoteKind, q)}
      >
        <Tab value="character" label={dm ? "Shared with you" : "Mine"} />
        <Tab value="party" label="Party" />
      </TabList>

      <HStack gap={2} vAlign="end">
        <TextInput
          type="search"
          label="Search notes"
          value={typed}
          onChange={(value) => setTyped(value.slice(0, SEARCH_MAX))}
          onEnter={() => show(kind, typed)}
          width="100%"
        />
        <StackItem size="static">
          <Button label="Search" onClick={() => show(kind, typed)} />
        </StackItem>
      </HStack>

      {canAdd && <AddNote key={kind} campaignId={campaign.id} kind={kind} />}

      {/* A failed read shows the failure, never an empty list. */}
      <Loaded query={notes} what="these notes">
        {(rows) =>
          rows.length === 0 ? (
            <Text type="body" color="secondary">
              {emptyText(kind, dm, q !== "")}
            </Text>
          ) : (
            <VStack gap={4} as="ul" className="note-list" aria-label="Notes">
              {rows.map((note) => (
                <li key={note.id}>
                  <NoteCard campaignId={campaign.id} note={note} />
                </li>
              ))}
            </VStack>
          )
        }
      </Loaded>
    </VStack>
  );
}
