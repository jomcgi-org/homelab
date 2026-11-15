import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { HStack, StackItem, VStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useAnnounce } from "@astryxdesign/core/hooks";
import { postEvent, afterCommand } from "../../api/session";
import { signIn } from "../../app/signIn";
import { useAction } from "../../app/useAction";
import { FREE_LABEL, presetSpeakers } from "../../lib/read-aloud.js";
import { composeMessage } from "../../lib/session-compose.js";
import {
  dropDraft,
  keepDraft,
  replyStillAvailable,
  restoreDraft,
  takeDraft,
} from "../../lib/session-expiry.js";
import type { Table } from "./useTable";

/** A private action the DM is answering. */
export interface ReplyTarget {
  sessionId: string;
  id: string;
  name: string;
  characterId: string;
}

interface Draft {
  sessionId: string;
  text: string;
  audience: string;
  replyTo: Pick<ReplyTarget, "id" | "name"> | null;
  resolved: boolean;
  speakerKey: string;
  speakerLabel: string;
  pending: { signature: string; id: string } | null;
}

/** Lives above table loading, but disappears with the viewer privacy boundary. */
export interface ComposerMemory {
  campaignId: string;
  viewer: { id: string; role: string; characterId: string | null };
  draft: Draft;
}

interface ComposerProps {
  table: Table;
  sessionId: string;
  /** Set by the feed's "Reply privately" button; cleared here. */
  reply: ReplyTarget | null;
  onReplyDone: () => void;
  memory: RefObject<ComposerMemory | null>;
  /** Extra actions shown beside Send: dice, handouts. */
  tools?: React.ReactNode;
}

const SESSION_CHANGED =
  "The session changed. Your text was kept for the DM. Choose who should receive it before sending.";
const RECIPIENT_CHANGED =
  "The recipient is no longer available. Your text was kept for the DM. Choose who should receive it before sending.";

const SPEAKER_LABEL = /^(?!\.+$)[A-Za-z0-9 _'.-]{1,64}$/;

/**
 * The message box. A send carries a request id, so a retry after a dropped
 * connection cannot post twice. When the sign-in lapses the draft is kept in
 * this tab and restored after the reload, never to a different viewer.
 */
export function Composer({
  table,
  sessionId,
  reply: selectedReply,
  onReplyDone,
  memory,
  tools,
}: ComposerProps) {
  const { viewer, characters, voices } = table;
  const campaignId = viewer.campaign.id;
  const dm = viewer.dm;
  const [remembered] = useState(() => memory.current);
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
  const [draftSessionId, setDraftSessionId] = useState(sessionId);
  const sessionChanged =
    draftSessionId !== sessionId ||
    (selectedReply !== null && selectedReply.sessionId !== sessionId);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const currentSession = useRef(sessionId);
  currentSession.current = sessionId;
  const [notice, setNotice] = useState("");
  const announce = useAnnounce();
  const [audience, setAudience] = useState("table");
  const [speakerKey, setSpeakerKey] = useState("narrator");
  const [speakerLabel, setSpeakerLabel] = useState("");
  const [resolved, setResolved] = useState(true);
  const [restoredReply, setRestoredReply] = useState<ReplyTarget | null>(null);
  const candidateReply = selectedReply ?? restoredReply;
  const recipientChanged =
    !sessionChanged &&
    candidateReply !== null &&
    !replyStillAvailable(candidateReply, {
      campaignId,
      sessionId,
      characterId: candidateReply.characterId,
      events: table.events,
      members: table.members,
    });
  const contextChanged = sessionChanged || recipientChanged;
  const reply = contextChanged ? null : candidateReply;
  const pending = useRef<{ signature: string; id: string } | null>(null);
  const finishReply = () => {
    pending.current = null;
    setRestoredReply(null);
    onReplyDone();
  };
  const liveSignature = useRef("");
  const input = useRef<HTMLTextAreaElement>(null);

  const send = useAction(postEvent, {
    invalidate: afterCommand(campaignId, sessionId),
    onSuccess: (_event, sent) => {
      if (!mounted.current || sent.sessionId !== currentSession.current) return;
      // Identical text can belong to a newly selected private reply. Clear
      // only the request whose full composer context still matches.
      if (
        pending.current !== null &&
        pending.current.id === sent.event.request_id &&
        pending.current.signature === liveSignature.current
      ) {
        setText("");
        setNotice("");
        pending.current = null;
        if (reply) finishReply();
        dropDraft(campaignId);
      }
    },
  });

  const snapshot = (): Draft => ({
    sessionId,
    text,
    audience: contextChanged
      ? "dm"
      : reply
        ? `pc:${reply.characterId}`
        : audience,
    replyTo: reply ? { id: reply.id, name: reply.name } : null,
    resolved: !contextChanged && resolved,
    speakerKey,
    speakerLabel,
    pending: contextChanged ? null : pending.current,
  });
  useLayoutEffect(() => {
    memory.current = {
      campaignId,
      viewer: {
        id: viewer.user.id,
        role: viewer.campaign.role,
        characterId: viewer.campaign.player_character_id,
      },
      draft: snapshot(),
    };
  });
  const live = useRef(snapshot);
  live.current = snapshot;
  const busy = useRef(false);
  busy.current = send.isPending;

  // Restore a draft kept across a sign-in reload, once.
  useEffect(() => {
    const owner = { id: viewer.user.id, role: viewer.campaign.role };
    const context = {
      campaignId,
      sessionId,
      dm,
      characters,
      speakers: speakerKeys,
      events: table.events,
      members: table.members,
    };
    const sameViewer =
      remembered?.campaignId === campaignId &&
      remembered.viewer.id === owner.id &&
      remembered.viewer.role === owner.role &&
      remembered.viewer.characterId === viewer.campaign.player_character_id;
    const kept = (
      sameViewer
        ? restoreDraft(
            { ...remembered.draft, viewer: remembered.viewer },
            owner,
            context,
          )
        : takeDraft(campaignId, owner, context)
    ) as
      | (ReturnType<typeof snapshot> & {
          resetReason: "session" | "recipient" | null;
        })
      | null;
    if (!kept) return;
    setText(kept.text);
    setAudience(kept.audience);
    if (kept.replyTo)
      setRestoredReply({
        ...kept.replyTo,
        sessionId,
        characterId: kept.audience.slice(3),
      });
    setResolved(kept.resolved);
    setSpeakerKey(kept.speakerKey);
    setSpeakerLabel(kept.speakerLabel);
    pending.current = kept.pending;
    if (kept.resetReason)
      setNotice(
        kept.resetReason === "session" ? SESSION_CHANGED : RECIPIENT_CHANGED,
      );
  }, [campaignId]);

  // A cached next session can replace this table without remounting the box.
  // Keep the text, but never attach an old action or retry to the new session.
  useEffect(() => {
    if (!contextChanged) return;
    setDraftSessionId(sessionId);
    finishReply();
    setAudience("dm");
    setResolved(false);
    pending.current = null;
    if (text.trim())
      setNotice(sessionChanged ? SESSION_CHANGED : RECIPIENT_CHANGED);
  }, [sessionId, contextChanged]);

  useEffect(() => {
    if (notice) announce(notice);
  }, [notice, announce]);

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
    if (selectedReply && !sessionChanged) {
      pending.current = null;
      setRestoredReply(null);
      setText("");
      setResolved(true);
      input.current?.focus();
    }
  }, [selectedReply]);

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
  const canSend =
    text.trim().length > 0 && labelValid && !send.isPending && !contextChanged;

  const body = text.trim();
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
  liveSignature.current = signature;

  const submit = () => {
    if (!canSend || !body) return;
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
      {notice && (
        <Text type="body" color="secondary">
          {notice}
        </Text>
      )}
      {reply && (
        <HStack gap={2} vAlign="center" wrap="wrap">
          <Text type="label">Replying privately to {reply.name}</Text>
          <Button
            label="Cancel reply"
            size="sm"
            variant="ghost"
            onClick={finishReply}
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
                  placement="above"
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
            {/* The composer is docked at the viewport's bottom edge, so its lists open upward. */}
            <Selector
              label="Send to"
              isLabelHidden
              placement="above"
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
              value={
                contextChanged
                  ? "dm"
                  : reply
                    ? `pc:${reply.characterId}`
                    : audience
              }
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
