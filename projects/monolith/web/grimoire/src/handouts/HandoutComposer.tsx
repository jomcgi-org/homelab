import { useRef, useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import {
  CheckboxList,
  CheckboxListItem,
} from "@astryxdesign/core/CheckboxList";
import { FileInput } from "@astryxdesign/core/FileInput";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useToast } from "@astryxdesign/core/Toast";
import { handoutEventFor, uploadHandoutImage } from "../api/handouts";
import type { HandoutUploadView, SessionEventRequest } from "../api/schema";
import { afterCommand, postEvent } from "../api/session";
import { useAction } from "../app/useAction";
import {
  HANDOUT_IMAGE_MAX_BYTES,
  HANDOUT_MARKDOWN_MAX,
  HANDOUT_TITLE_MAX,
} from "../lib/handout.js";
import type { TableCharacter } from "../screens/session/table";
import { HandoutText } from "./HandoutText";
import "./handouts.css";

export interface HandoutComposerProps {
  campaignId: string;
  sessionId: string;
  characters: TableCharacter[];
}

interface ChosenImage {
  file: File;
  /** Set once the backend has stored the file. */
  stored: HandoutUploadView | null;
}

/** The DM writes a handout, optionally with an image, for chosen characters. */
export function HandoutComposer({
  campaignId,
  sessionId,
  characters,
}: HandoutComposerProps) {
  const toast = useToast();
  const [title, setTitle] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [toTable, setToTable] = useState(true);
  const [picked, setPicked] = useState<string[]>([]);
  const [image, setImage] = useState<ChosenImage | null>(null);
  const [imageProblem, setImageProblem] = useState("");
  const pending = useRef<{ signature: string; id: string } | null>(null);

  const upload = useAction(uploadHandoutImage);
  const send = useAction(postEvent, {
    invalidate: afterCommand(campaignId, sessionId),
    success: "Handout sent.",
    onSuccess: () => {
      setTitle("");
      setMarkdown("");
      setToTable(true);
      setPicked([]);
      setImage(null);
      setImageProblem("");
      pending.current = null;
    },
  });

  // A character who left the table since being ticked is not a recipient.
  const recipients = picked.filter((id) =>
    characters.some((row) => row.id === id),
  );
  const ready = title.trim().length > 0 && (toTable || recipients.length > 0);
  const canSend = ready && !send.isPending && !upload.isPending;

  const chooseImage = async (chosen: File | File[] | null) => {
    if (upload.isPending) return;
    const file = Array.isArray(chosen) ? (chosen[0] ?? null) : chosen;
    setImageProblem("");
    setImage(null);
    if (!file) return;
    if (file.size > HANDOUT_IMAGE_MAX_BYTES) {
      setImageProblem("Handout images can be at most 5 MiB.");
      return;
    }
    setImage({ file, stored: null });
    const stored = await upload.run({ campaignId, file });
    // A refused upload leaves the field empty; the toast says why.
    setImage(stored ? { file, stored } : null);
  };

  const submit = () => {
    if (!canSend) return;
    const draft = {
      title,
      markdown,
      audience: toTable ? ("table" as const) : recipients,
      imageKey: image?.stored?.key ?? null,
    };
    // A retry of the same content reuses its request id so it cannot post twice.
    const signature = JSON.stringify({ sessionId, ...draft });
    if (pending.current?.signature !== signature)
      pending.current = { signature, id: crypto.randomUUID() };
    let event: SessionEventRequest;
    try {
      event = handoutEventFor({ ...draft, requestId: pending.current.id });
    } catch (problem) {
      toast({ body: (problem as Error).message, type: "error" });
      return;
    }
    send.run({ campaignId, sessionId, event });
  };

  return (
    <VStack gap={3} as="section" aria-label="Send a handout">
      <TextInput
        label="Handout title"
        placeholder="A map of the pass"
        value={title}
        onChange={(value) => setTitle(value.slice(0, HANDOUT_TITLE_MAX))}
        isRequired
        autoComplete="off"
      />
      <TextArea
        label="Handout text"
        description="Markdown: **bold**, *italic*, lists, > quotes, tables."
        value={markdown}
        onChange={(value) => setMarkdown(value.slice(0, HANDOUT_MARKDOWN_MAX))}
        rows={5}
      />
      <CheckboxInput
        label="Send to everyone at the table"
        value={toTable}
        onChange={setToTable}
      />
      {!toTable &&
        (characters.length > 0 ? (
          <CheckboxList label="Send to" value={picked} onChange={setPicked}>
            {characters.map((character) => (
              <CheckboxListItem
                key={character.id}
                value={character.id}
                label={character.character_name}
              />
            ))}
          </CheckboxList>
        ) : (
          <Text type="body" color="secondary">
            No player characters are at this table yet.
          </Text>
        ))}
      <FileInput
        label="Image"
        description="PNG, JPEG, GIF or WebP, up to 5 MiB."
        isOptional
        accept="image/png,image/jpeg,image/gif,image/webp"
        value={image?.file ?? null}
        onChange={chooseImage}
        isLoading={upload.isPending}
        status={
          imageProblem
            ? { type: "error", message: imageProblem }
            : image?.stored
              ? {
                  type: "success",
                  message: `Uploaded ${image.file.name} (${image.stored.content_type}, ${Math.ceil(image.stored.size / 1024)} KB).`,
                }
              : undefined
        }
        statusVariant="detached"
      />
      <VStack
        gap={2}
        as="section"
        aria-label="Handout preview"
        className="handout-preview"
      >
        <Text type="label" color="secondary">
          Preview
        </Text>
        <Heading level={3}>{title.trim() || "Untitled handout"}</Heading>
        <HandoutText markdown={markdown} />
      </VStack>
      <HStack>
        <Button
          label="Send handout"
          variant="primary"
          isLoading={send.isPending}
          isDisabled={!canSend}
          onClick={submit}
        />
      </HStack>
    </VStack>
  );
}
