import { useState } from "react";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { deleteNote, noteKeys, updateNote, type Note } from "../../api/notes";
import { useAction } from "../../app/useAction";
import { NoteText } from "../../notes/NoteText";
import { TEXT_MAX, TITLE_MAX } from "./AddNote";

const sharingOptions = [
  { value: "private", label: "Private" },
  { value: "shared", label: "Share with DM" },
];

/** One note as this viewer may read it, with editing where they may edit. */
export function NoteCard({
  campaignId,
  note,
}: {
  campaignId: string;
  note: Note;
}) {
  // Only the author of a character note sees or changes its DM sharing.
  const ownsSharing = note.kind === "character" && note.is_mine;
  return (
    <VStack gap={2} as="article" id={`note-${note.id}`} aria-label={note.title}>
      <HStack gap={2} vAlign="center" wrap="wrap">
        <Heading level={2}>{note.title}</Heading>
        {note.pinned && <Badge label="Pinned" />}
      </HStack>
      {ownsSharing && (
        <Text type="body" size="sm" color="secondary">
          {note.dm_readable ? "Shared with DM" : "Private"}
        </Text>
      )}
      <NoteText text={note.markdown} />
      {note.links.entities.length > 0 && (
        <HStack
          gap={2}
          wrap="wrap"
          as="ul"
          className="note-list"
          aria-label="Linked entities"
        >
          {note.links.entities.map((entity) => (
            <li key={entity.id}>
              <Badge label={entity.name} />
            </li>
          ))}
        </HStack>
      )}
      {note.can_edit && (
        <Collapsible
          defaultIsOpen={false}
          trigger={<Text type="label">Edit note</Text>}
        >
          <EditNote
            campaignId={campaignId}
            note={note}
            ownsSharing={ownsSharing}
          />
        </Collapsible>
      )}
    </VStack>
  );
}

function EditNote({
  campaignId,
  note,
  ownsSharing,
}: {
  campaignId: string;
  note: Note;
  ownsSharing: boolean;
}) {
  const [title, setTitle] = useState(note.title);
  const [markdown, setMarkdown] = useState(note.markdown);
  const [shared, setShared] = useState(note.dm_readable === true);
  const [deleting, setDeleting] = useState(false);
  const invalidate = [noteKeys.all(campaignId)];
  const save = useAction(updateNote, { invalidate, success: "Note saved." });
  const remove = useAction(deleteNote, {
    invalidate,
    success: "Note deleted.",
  });
  return (
    <VStack gap={3} paddingBlockStart={3}>
      <TextInput
        label="Note title"
        isRequired
        value={title}
        onChange={(value) => setTitle(value.slice(0, TITLE_MAX))}
        width="100%"
      />
      <TextArea
        label="Note text"
        rows={5}
        maxLength={TEXT_MAX}
        value={markdown}
        onChange={setMarkdown}
        width="100%"
      />
      {ownsSharing && (
        <Selector
          label="DM sharing"
          options={sharingOptions}
          value={shared ? "shared" : "private"}
          onChange={(value) => setShared(value === "shared")}
          width="100%"
        />
      )}
      <HStack gap={2} wrap="wrap">
        <Button
          label="Save changes"
          variant="primary"
          isLoading={save.isPending}
          isDisabled={!title.trim() || remove.isPending}
          onClick={() =>
            save.run({
              campaignId,
              noteId: note.id,
              changes: {
                title: title.trim(),
                markdown,
                ...(ownsSharing ? { dm_readable: shared } : {}),
              },
            })
          }
        />
        <Button
          label="Delete note"
          variant="ghost"
          isDisabled={save.isPending}
          onClick={() => setDeleting(true)}
        />
      </HStack>
      <AlertDialog
        isOpen={deleting}
        onOpenChange={setDeleting}
        title="Delete this note?"
        description={`"${note.title}" is removed for everyone who can read it.`}
        actionLabel="Delete note"
        isActionLoading={remove.isPending}
        onAction={() => remove.run({ campaignId, noteId: note.id })}
      />
    </VStack>
  );
}
