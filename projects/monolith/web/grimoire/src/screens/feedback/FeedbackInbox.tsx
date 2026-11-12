import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { ApiError, SignInError } from "../../api/client";
import {
  dismissFeedback,
  fileFeedback,
  keys,
  useFeedbackInbox,
} from "../../api/feedback";
import { keys as lobbyKeys } from "../../api/lobby";
import type { FeedbackView } from "../../api/schema";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import { useAction } from "../../app/useAction";
import { FormDialog } from "../../inventory/FormDialog";
import { NoteText } from "../../notes/NoteText";

const TITLE_MAX = 120;
const BODY_MAX = 4000;
const changed = [keys.inbox, lobbyKeys.lobby];

/** The operator's private inbox: file each item as an issue, or dismiss it. */
export function FeedbackInbox() {
  const inbox = useFeedbackInbox();
  const refused =
    inbox.data === undefined &&
    inbox.error instanceof ApiError &&
    !(inbox.error instanceof SignInError) &&
    inbox.error.status === 403;
  return (
    <Page
      title="Feedback inbox"
      back={{ href: paths.lobby, label: "Back to Grimoire" }}
    >
      {refused ? (
        <EmptyState title="The feedback inbox is for operators." />
      ) : (
        <Loaded query={inbox} what="the feedback">
          {(items) => <Inbox items={items} />}
        </Loaded>
      )}
    </Page>
  );
}

function Inbox({ items }: { items: FeedbackView[] }) {
  const [filing, setFiling] = useState<FeedbackView | null>(null);
  const dismiss = useAction(dismissFeedback, {
    invalidate: changed,
    success: "Dismissed.",
  });
  if (items.length === 0) {
    return <EmptyState isCompact title="No new feedback." />;
  }
  return (
    <>
      {items.map((item) => (
        <Card key={item.id}>
          <VStack
            gap={3}
            as="article"
            aria-label={`Feedback from ${item.author}`}
          >
            <NoteText text={item.text} />
            <Text type="supporting">
              {item.author} ·{" "}
              {item.campaign_name ? `${item.campaign_name} · ` : ""}
              {item.page} ·{" "}
              <Timestamp
                value={item.created_at}
                format="date_time"
                type="inherit"
                color="inherit"
                hasTooltip={false}
              />
            </Text>
            <HStack gap={2} wrap="wrap">
              <Button label="File issue" onClick={() => setFiling(item)} />
              <Button
                label="Dismiss"
                variant="ghost"
                isDisabled={dismiss.isPending}
                onClick={() => dismiss.run(item.id)}
              />
            </HStack>
          </VStack>
        </Card>
      ))}
      {filing && (
        <FileIssue
          key={filing.id}
          item={filing}
          onClose={() => setFiling(null)}
        />
      )}
    </>
  );
}

/**
 * The repository is public, so the operator writes what is published. The
 * body starts as the sender's words to edit, never to send unread.
 */
function FileIssue({
  item,
  onClose,
}: {
  item: FeedbackView;
  onClose: () => void;
}) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState(item.text);
  const file = useAction(fileFeedback, {
    invalidate: changed,
    success: (issue) => `Filed as issue #${issue.issue_number}.`,
    onSuccess: onClose,
  });
  const cleanTitle = title.trim();
  const cleanBody = body.trim();
  const invalid = !cleanTitle || !cleanBody || cleanBody.length > BODY_MAX;
  const submit = () => {
    if (!invalid) file.run({ id: item.id, title: cleanTitle, body: cleanBody });
  };
  return (
    <FormDialog
      title="File issue"
      submitLabel="File issue"
      isPending={file.isPending}
      isInvalid={invalid}
      onSubmit={submit}
      onClose={onClose}
    >
      <TextInput
        label="Title"
        isRequired
        value={title}
        onChange={(value) => setTitle(value.slice(0, TITLE_MAX))}
        onEnter={submit}
        width="100%"
        hasAutoFocus
      />
      <TextArea
        label="Body"
        value={body}
        onChange={setBody}
        maxLength={BODY_MAX}
        rows={8}
        width="100%"
      />
      <Text type="supporting">
        The title and body are published on the public repository. The
        sender&apos;s name is not.
      </Text>
    </FormDialog>
  );
}
