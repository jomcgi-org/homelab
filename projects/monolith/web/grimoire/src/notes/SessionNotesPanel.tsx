import { useRef, useState } from "react";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import {
  createNote,
  deleteNote,
  noteKeys,
  updateNote,
  useNotes,
  type Note,
  type NoteKind,
} from "../api/notes";
import { Loaded } from "../app/Loaded";
import { useAction } from "../app/useAction";
import { NoteText } from "./NoteText";
import "./notes.css";

export interface SessionNotesPanelProps {
  campaignId: string;
  dm: boolean;
  onShowEvent: (eventId: string) => void;
  onOpenKnowledge: (entityId: string) => void;
}

/** Other players add party notes during play; this is how soon they appear. */
const REFRESH_MS = 15_000;
const TITLE_MAX = 200;
const TEXT_MAX = 20_000;

interface Draft {
  /** The note being edited, or null while writing a new one. */
  noteId: string | null;
  title: string;
  markdown: string;
  dmReadable: boolean;
  pinned: boolean;
}

const blank: Draft = {
  noteId: null,
  title: "",
  markdown: "",
  dmReadable: false,
  pinned: false,
};

const audience = (kind: NoteKind, dm: boolean) =>
  kind === "party"
    ? "Shared with the DM and every player with a character."
    : dm
      ? "Character notes players explicitly shared with you."
      : "Only you can read these unless you share a note with the DM.";

/** Character and party notes beside the feed, including pinned events. */
export function SessionNotesPanel({
  campaignId,
  dm,
  onShowEvent,
  onOpenKnowledge,
}: SessionNotesPanelProps) {
  const [kind, setKind] = useState<NoteKind>("character");
  const [draft, setDraft] = useState<Draft>(blank);
  const [deleting, setDeleting] = useState<Note | null>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const notes = useNotes(campaignId, kind, { refetchInterval: REFRESH_MS });
  const invalidate = [noteKeys.all(campaignId)];

  // Only a character note's author may set its DM sharing; the backend
  // refuses the field on a party note, so it is sent for character notes only.
  const sharing = kind === "character" ? { dm_readable: draft.dmReadable } : {};
  const fields = {
    title: draft.title.trim(),
    markdown: draft.markdown,
    pinned: draft.pinned,
    ...sharing,
  };
  const save = useAction(
    () =>
      draft.noteId
        ? updateNote({ campaignId, noteId: draft.noteId, changes: fields })
        : createNote({ campaignId, note: { kind, ...fields } }),
    {
      invalidate,
      success: "Note saved.",
      onSuccess: () => setDraft(blank),
    },
  );
  const remove = useAction(deleteNote, {
    invalidate,
    success: "Note deleted.",
    onSuccess: (_result, input) => {
      setDeleting(null);
      setDraft((current) =>
        current.noteId === input.noteId ? blank : current,
      );
    },
  });
  const busy = save.isPending || remove.isPending;
  // A DM reads shared character notes but writes only party notes.
  const canWrite = !dm || kind === "party";
  const complete = Boolean(draft.title.trim() && draft.markdown.trim());

  const edit = (note: Note) => {
    setDraft({
      noteId: note.id,
      title: note.title,
      markdown: note.markdown,
      dmReadable: note.dm_readable === true,
      pinned: note.pinned,
    });
    // The form sits above the list; take the writer to it.
    titleInput.current?.focus();
  };

  return (
    <VStack gap={5} as="section" aria-label="Campaign notes">
      <TabList
        aria-label="Notes audience"
        value={kind}
        onChange={(value) => {
          setKind(value as NoteKind);
          setDraft(blank);
        }}
      >
        <Tab value="character" label={dm ? "Shared with you" : "My notes"} />
        <Tab value="party" label="Party notes" />
      </TabList>
      <Text type="body" color="secondary">
        {audience(kind, dm)}
      </Text>

      {canWrite && (
        <VStack gap={3}>
          <Heading level={2}>
            {draft.noteId ? "Edit note" : "Add a note"}
          </Heading>
          <TextInput
            ref={titleInput}
            label="Note title"
            isRequired
            isDisabled={busy}
            value={draft.title}
            onChange={(value) =>
              setDraft({ ...draft, title: value.slice(0, TITLE_MAX) })
            }
            width="100%"
          />
          <TextArea
            label="Note text"
            isRequired
            isDisabled={busy}
            rows={5}
            maxLength={TEXT_MAX}
            value={draft.markdown}
            onChange={(value) => setDraft({ ...draft, markdown: value })}
            width="100%"
          />
          {kind === "character" && (
            <CheckboxInput
              label="Share this note with the DM"
              isDisabled={busy}
              value={draft.dmReadable}
              onChange={(checked) =>
                setDraft({ ...draft, dmReadable: checked })
              }
            />
          )}
          <CheckboxInput
            label="Keep pinned"
            isDisabled={busy}
            value={draft.pinned}
            onChange={(checked) => setDraft({ ...draft, pinned: checked })}
          />
          <HStack gap={2} wrap="wrap">
            <Button
              label="Save note"
              variant="primary"
              isLoading={save.isPending}
              isDisabled={!complete || remove.isPending}
              onClick={() => save.run(undefined)}
            />
            {draft.noteId && (
              <Button
                label="Cancel edit"
                variant="ghost"
                isDisabled={busy}
                onClick={() => setDraft(blank)}
              />
            )}
          </HStack>
        </VStack>
      )}

      <Loaded query={notes} what="these notes">
        {(rows) =>
          rows.length === 0 ? (
            <Text type="body" color="secondary">
              No notes here yet.
            </Text>
          ) : (
            <VStack
              gap={4}
              as="ul"
              className="note-list"
              aria-label={kind === "party" ? "Party notes" : "Character notes"}
            >
              {rows.map((note) => (
                <VStack gap={2} as="li" key={note.id}>
                  <HStack gap={2} vAlign="center" wrap="wrap">
                    <Heading level={3}>{note.title}</Heading>
                    {note.pinned && <Badge label="Pinned" />}
                  </HStack>
                  <NoteText text={note.markdown} />
                  {note.kind === "character" && note.dm_readable && (
                    <Text type="body" size="sm" color="secondary">
                      Shared with the DM
                    </Text>
                  )}
                  <HStack gap={2} wrap="wrap">
                    {note.links.event_ids.map((eventId) => (
                      <Button
                        key={eventId}
                        label="Source event"
                        size="sm"
                        onClick={() => onShowEvent(eventId)}
                      />
                    ))}
                    {note.links.entities.map((entity) => (
                      <Button
                        key={entity.id}
                        label={entity.name || "Related knowledge"}
                        size="sm"
                        onClick={() => onOpenKnowledge(entity.id)}
                      />
                    ))}
                    {note.can_edit && (
                      <>
                        <Button
                          label="Edit"
                          aria-label={`Edit ${note.title}`}
                          size="sm"
                          variant="ghost"
                          isDisabled={busy}
                          onClick={() => edit(note)}
                        />
                        <Button
                          label="Delete"
                          aria-label={`Delete ${note.title}`}
                          size="sm"
                          variant="ghost"
                          isDisabled={busy}
                          onClick={() => setDeleting(note)}
                        />
                      </>
                    )}
                  </HStack>
                </VStack>
              ))}
            </VStack>
          )
        }
      </Loaded>

      <AlertDialog
        isOpen={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title="Delete this note?"
        description={`"${deleting?.title ?? ""}" is removed for everyone who can read it.`}
        actionLabel="Delete note"
        isActionLoading={remove.isPending}
        onAction={() =>
          deleting && remove.run({ campaignId, noteId: deleting.id })
        }
      />
    </VStack>
  );
}
