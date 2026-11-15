import { useId, useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { createNote, noteKeys, type NoteKind } from "../../api/notes";
import { useAction } from "../../app/useAction";

export const TITLE_MAX = 200;
export const TEXT_MAX = 20_000;

/** "default" leaves the choice to the campaign's setting. */
type Sharing = "default" | "private" | "shared";

const sharingOptions = [
  { value: "default", label: "Campaign default" },
  { value: "private", label: "Private" },
  { value: "shared", label: "Share with DM" },
];

/** Write a new note of the kind the open tab shows. */
export function AddNote({
  campaignId,
  kind,
}: {
  campaignId: string;
  kind: NoteKind;
}) {
  const headingId = useId();
  const [title, setTitle] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [sharing, setSharing] = useState<Sharing>("default");
  const add = useAction(createNote, {
    invalidate: [noteKeys.all(campaignId)],
    success: "Note added.",
    onSuccess: () => {
      setTitle("");
      setMarkdown("");
      setSharing("default");
    },
  });
  const submit = () =>
    add.run({
      campaignId,
      note: {
        kind,
        title: title.trim(),
        markdown,
        ...(kind === "character" && sharing !== "default"
          ? { dm_readable: sharing === "shared" }
          : {}),
      },
    });
  return (
    <VStack gap={3} as="section" aria-labelledby={headingId}>
      <Heading level={2} id={headingId}>
        Add a note
      </Heading>
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
      {kind === "character" ? (
        <Selector
          label="DM sharing"
          options={sharingOptions}
          value={sharing}
          onChange={(value) => setSharing(value as Sharing)}
          width="100%"
        />
      ) : (
        <Text type="body" color="secondary">
          Party notes are shared with the DM and players with a character.
        </Text>
      )}
      <HStack>
        <Button
          label="Add note"
          variant="primary"
          isLoading={add.isPending}
          isDisabled={!title.trim()}
          onClick={submit}
        />
      </HStack>
    </VStack>
  );
}
