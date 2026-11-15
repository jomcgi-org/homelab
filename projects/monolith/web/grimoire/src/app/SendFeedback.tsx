import { useRef, useState } from "react";
import { useLocation, useMatch } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@astryxdesign/core/Button";
import { Link } from "@astryxdesign/core/Link";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { keys as feedbackKeys, sendFeedback } from "../api/feedback";
import { keys as lobbyKeys } from "../api/lobby";
import { FormDialog } from "../inventory/FormDialog";
import { paths } from "./paths";
import { useAction } from "./useAction";
import { useSignIn } from "./useSignIn";

const TEXT_MAX = 2000;
const PAGE_MAX = 200;
const UUID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

/**
 * Where the unsent draft is kept. Each page mounts its own dialog, so the
 * draft lives in the query cache to survive following the roadmap link and
 * coming back.
 */
const DRAFT = ["feedback", "draft"] as const;

/**
 * The quiet "Feedback" action in every page header. It records where it was
 * sent from, so nobody has to describe the screen they were on. The draft is
 * kept while the page stays open, so a stray Escape loses nothing. A note is
 * shown to everyone on the roadmap, and the dialog says so before it is sent.
 */
export function SendFeedback() {
  const signedOut = useSignIn() === "signed-out";
  const { pathname } = useLocation();
  const campaignId = useMatch("/campaigns/:campaignId/*")?.params.campaignId;
  const [open, setOpen] = useState(false);
  const client = useQueryClient();
  const [text, setDraft] = useState(
    () => client.getQueryData<string>(DRAFT) ?? "",
  );
  const setText = (value: string) => {
    client.setQueryData(DRAFT, value);
    setDraft(value);
  };
  const trigger = useRef<HTMLButtonElement>(null);
  const close = () => {
    setOpen(false);
    // The conditional dialog unmounts on close. Restore after its removal,
    // including the library's own modal cleanup and descendant autofocus.
    requestAnimationFrame(() => trigger.current?.focus());
  };
  const send = useAction(sendFeedback, {
    invalidate: [feedbackKeys.inbox, feedbackKeys.roadmap, lobbyKeys.lobby],
    success: "Thanks. Sent.",
    onSuccess: () => {
      setText("");
      close();
    },
  });
  if (signedOut) return null;
  const trimmed = text.trim();
  return (
    <>
      <Button
        ref={trigger}
        label="Feedback"
        variant="ghost"
        onClick={() => setOpen(true)}
      />
      {open && (
        <FormDialog
          title="Feedback"
          submitLabel="Send"
          isPending={send.isPending}
          isInvalid={!trimmed || trimmed.length > TEXT_MAX}
          onClose={close}
          onSubmit={() =>
            send.run({
              text: trimmed,
              page: pathname.slice(0, PAGE_MAX),
              campaignId:
                campaignId && UUID.test(campaignId) ? campaignId : undefined,
            })
          }
        >
          <TextArea
            label="What should change?"
            value={text}
            onChange={setText}
            maxLength={TEXT_MAX}
            rows={6}
            width="100%"
            hasAutoFocus
          />
          <Text type="supporting">
            Everyone signed in can see this.{" "}
            <Link href={paths.roadmap} onClick={close} size="sm">
              See what&apos;s already suggested
            </Link>
          </Text>
        </FormDialog>
      )}
    </>
  );
}
