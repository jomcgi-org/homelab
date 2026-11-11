import { useEffect, useRef, useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { HStack, StackItem, VStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { postEvent, afterCommand } from "../../api/session";
import { signIn } from "../../app/signIn";
import { useAction } from "../../app/useAction";
import { FREE_LABEL, presetSpeakers } from "../../lib/read-aloud.js";
import { composeMessage } from "../../lib/session-compose.js";
import { dropDraft, keepDraft, takeDraft } from "../../lib/session-expiry.js";
import type { Table } from "./useTable";

/** A private action the DM is answering. */
export interface ReplyTarget {
  id: string;
  name: string;
  characterId: string;
}

interface ComposerProps {
  table: Table;
  sessionId: string;
  /** Set by the feed's "Reply privately" button; cleared here. */
  reply: ReplyTarget | null;
  onReplyDone: () => void;
  /** Extra actions shown beside Send: dice, handouts. */
  tools?: React.ReactNode;
}

const SPEAKER_LABEL = /^(?!\.+$)[A-Za-z0-9 _'.-]{1,64}$/;

/**
 * The message box. A send carries a request id, so a retry after a dropped
 * connection cannot post twice. When the sign-in lapses the draft is kept in
 * this tab and restored after the reload, never to a different viewer.
 */
export function Composer({
  table,
  sessionId,
  reply,
  onReplyDone,
  tools,
}: ComposerProps) {
  const { viewer, characters, voices } = table;
  const campaignId = viewer.campaign.id;
  const dm = viewer.dm;
  const speakers = presetSpeakers(voices) as Array<{
    key: string;
    name: string;
  }>;
  const speakerKeys = [
    "narrator",
    ...speakers.map((row) => row.key),
    FREE_LABEL,
  ];

  const [text, setText] = useState("");
  const [audience, setAudience] = useState("table");
  const [speakerKey, setSpeakerKey] = useState("narrator");
  const [speakerLabel, setSpeakerLabel] = useState("");
  const [resolved, setResolved] = useState(true);
  const pending = useRef<{ signature: string; id: string } | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);

  const send = useAction(postEvent, {
    invalidate: afterCommand(campaignId, sessionId),
    onSuccess: (_event, sent) => {
      // Clear only if the box still holds what was sent (the body is trimmed).
      if (text.trim() === (sent.event.body as { text: string }).text) {
        setText("");
        pending.current = null;
        if (reply) onReplyDone();
        dropDraft(campaignId);
      }
    },
  });

  const snapshot = () => ({
    text,
    audience: reply ? `pc:${reply.characterId}` : audience,
    replyTo: reply ? { id: reply.id, name: reply.name } : null,
    resolved,
    speakerKey,
    speakerLabel,
    pending: pending.current,
  });
  const live = useRef(snapshot);
  live.current = snapshot;
  const busy = useRef(false);
  busy.current = send.isPending;

  // Restore a draft kept across a sign-in reload, once.
  useEffect(() => {
    const kept = takeDraft(
      campaignId,
      { id: viewer.user.id, role: viewer.campaign.role },
      { dm, characters, speakers: speakerKeys },
    ) as ReturnType<typeof snapshot> | null;
    if (!kept) return;
    setText(kept.text);
    setAudience(kept.audience);
    setResolved(kept.resolved);
    setSpeakerKey(kept.speakerKey);
    setSpeakerLabel(kept.speakerLabel);
    pending.current = kept.pending;
  }, [campaignId]);

  // Before an automatic reload: hold it while a send is in flight (a message
  // that landed must not come back as a draft), otherwise keep the draft.
  useEffect(
    () =>
      signIn.beforeReload(() => {
        if (busy.current) return false;
        keepDraft(
          campaignId,
          { id: viewer.user.id, role: viewer.campaign.role },
          live.current(),
        );
        return true;
      }),
    [campaignId, viewer.user.id, viewer.campaign.role],
  );

  // A deleted voice preset leaves the picker; fall back to the narrator.
  useEffect(() => {
    if (!speakerKeys.includes(speakerKey)) setSpeakerKey("narrator");
  }, [speakerKeys.join("|")]);

  useEffect(() => {
    if (reply) {
      setText("");
      setResolved(true);
      input.current?.focus();
    }
  }, [reply]);

  // Ctrl or Cmd with Enter sends. Enter alone stays a new line, and an Enter
  // that confirms an IME composition is never a send.
  const submitRef = useRef<() => void>(() => {});
  useEffect(() => {
    const box = input.current;
    if (!box) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.key === "Enter" &&
        (event.ctrlKey || event.metaKey) &&
        !event.isComposing
      ) {
        event.preventDefault();
        submitRef.current();
      }
    };
    box.addEventListener("keydown", onKeyDown);
    return () => box.removeEventListener("keydown", onKeyDown);
  }, []);

  const label = speakerLabel.trim();
  const labelValid = speakerKey !== FREE_LABEL || SPEAKER_LABEL.test(label);
  const canSend = text.trim().length > 0 && labelValid && !send.isPending;

  const submit = () => {
    const body = text.trim();
    if (!canSend || !body) return;
    const message = composeMessage({
      text: body,
      dm,
      audience: reply ? `pc:${reply.characterId}` : audience,
      replyTo: reply,
      resolved,
      speakerKey: speakerKey === FREE_LABEL ? label : speakerKey,
    }) as {
      kind: "narration" | "action";
      audience: "table" | "dm" | "pcs";
      pcIds: string[];
      replyTo?: string;
      resolved: boolean;
      speakerKey?: string;
    };
    const signature = JSON.stringify({ sessionId, ...message, text: body });
    if (pending.current?.signature !== signature)
      pending.current = { signature, id: crypto.randomUUID() };
    send.run({
      campaignId,
      sessionId,
      event: {
        kind: message.kind,
        audience: message.audience,
        audience_pc_ids: message.pcIds,
        request_id: pending.current.id,
        body: {
          text: body,
          ...(message.kind === "narration" && message.replyTo
            ? { reply_to: message.replyTo, resolved: message.resolved }
            : {}),
          ...(message.kind === "narration" && message.speakerKey !== undefined
            ? { speaker_key: message.speakerKey }
            : {}),
        },
      },
    });
  };

  submitRef.current = submit;

  return (
    <VStack gap={3} as="section" aria-label="Message">
      {reply && (
        <HStack gap={2} vAlign="center" wrap="wrap">
          <Text type="label">Replying privately to {reply.name}</Text>
          <Button
            label="Cancel reply"
            size="sm"
            variant="ghost"
            onClick={onReplyDone}
          />
        </HStack>
      )}
      <TextArea
        ref={input}
        label={dm ? "Set the scene" : "What do you do?"}
        isLabelHidden
        placeholder={
          dm
            ? "The lantern flickers as someone knocks…"
            : "I approach the door…"
        }
        value={text}
        onChange={(value) => setText(value.slice(0, 8000))}
        rows={2}
      />
      <HStack gap={2} vAlign="end" wrap="wrap">
        <StackItem size="fill">
          <HStack gap={2} wrap="wrap" vAlign="end">
            {dm && (
              <>
                <Selector
                  label="Narration speaker"
                  isLabelHidden
                  options={[
                    { value: "narrator", label: "Narrator" },
                    ...speakers.map((row) => ({
                      value: row.key,
                      label: row.name,
                    })),
                    { value: FREE_LABEL, label: "Free label" },
                  ]}
                  value={speakerKey}
                  onChange={setSpeakerKey}
                  width={160}
                />
                {speakerKey === FREE_LABEL && (
                  <TextInput
                    label="Speaker label"
                    isLabelHidden
                    placeholder="Speaker label"
                    value={speakerLabel}
                    onChange={(value) => setSpeakerLabel(value.slice(0, 64))}
                    status={
                      label && !labelValid
                        ? {
                            type: "error",
                            message:
                              "Letters, numbers, spaces and _ ' . - only.",
                          }
                        : undefined
                    }
                    width={160}
                  />
                )}
              </>
            )}
            <Selector
              label="Send to"
              isLabelHidden
              isDisabled={reply !== null}
              options={[
                { value: "table", label: "Everyone" },
                { value: "dm", label: dm ? "DM notes" : "DM privately" },
                ...(dm
                  ? characters.map((row) => ({
                      value: `pc:${row.id}`,
                      label: `${row.character_name} and DM`,
                    }))
                  : []),
              ]}
              value={reply ? `pc:${reply.characterId}` : audience}
              onChange={setAudience}
              width={190}
            />
          </HStack>
        </StackItem>
        <StackItem size="static">
          <HStack gap={2} wrap="wrap" hAlign="end">
            {tools}
            <Button
              label="Send"
              variant="primary"
              tooltip="Ctrl+Enter"
              isLoading={send.isPending}
              isDisabled={!canSend}
              onClick={submit}
            />
          </HStack>
        </StackItem>
      </HStack>
      {reply && (
        <CheckboxInput
          label="Mark action resolved"
          value={resolved}
          onChange={setResolved}
        />
      )}
    </VStack>
  );
}
