import { useState } from "react";
import { useLocation, useMatch } from "react-router";
import { Button } from "@astryxdesign/core/Button";
import { TextArea } from "@astryxdesign/core/TextArea";
import { keys as feedbackKeys, sendFeedback } from "../api/feedback";
import { keys as lobbyKeys } from "../api/lobby";
import { FormDialog } from "../inventory/FormDialog";
import { useAction } from "./useAction";
import { useSignIn } from "./useSignIn";

const TEXT_MAX = 2000;
const PAGE_MAX = 200;
const UUID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

/**
 * The quiet "Feedback" action in every page header. It records where it was
 * sent from, so nobody has to describe the screen they were on. The draft is
 * kept while the page stays open, so a stray Escape loses nothing.
 */
export function SendFeedback() {
  const signedOut = useSignIn() === "signed-out";
  const { pathname } = useLocation();
  const campaignId = useMatch("/campaigns/:campaignId/*")?.params.campaignId;
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const send = useAction(sendFeedback, {
    invalidate: [feedbackKeys.inbox, lobbyKeys.lobby],
    success: "Thanks. Sent.",
    onSuccess: () => {
      setText("");
      setOpen(false);
    },
  });
  if (signedOut) return null;
  const trimmed = text.trim();
  return (
    <>
      <Button label="Feedback" variant="ghost" onClick={() => setOpen(true)} />
      {open && (
        <FormDialog
          title="Feedback"
          submitLabel="Send"
          isPending={send.isPending}
          isInvalid={!trimmed || trimmed.length > TEXT_MAX}
          onClose={() => setOpen(false)}
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
        </FormDialog>
      )}
    </>
  );
}
