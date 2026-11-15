import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import {
  HStack,
  Layout,
  LayoutContent,
  LayoutFooter,
} from "@astryxdesign/core/Layout";
import type { Command, PlatformInvitation } from "../../api/platform";
import { ReasonField, useReason } from "./Reason";

const STATUS: Record<PlatformInvitation["status"], string> = {
  awaiting_delivery: "Link not shown yet",
  pending: "Link shown",
  accepted: "Accepted",
  revoked: "Revoked",
  expired: "Expired",
};

const expiry = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});

export const invitationSummary = (invitation: PlatformInvitation) =>
  `${STATUS[invitation.status] ?? invitation.status} · Expires ${expiry.format(
    new Date(invitation.expires_at),
  )} UTC`;

/** Only an unused, unexpired invitation can still be shown or revoked. */
export const isOpenInvitation = (invitation: PlatformInvitation) =>
  invitation.status === "awaiting_delivery" || invitation.status === "pending";

interface ManageInvitationProps {
  invitation: PlatformInvitation;
  isPending: boolean;
  onApply: (command: Command) => void;
  onClose: () => void;
}

/** Show an invitation's signup link, replace one already shown, or revoke it. */
export function ManageInvitation({
  invitation,
  isPending,
  onApply,
  onClose,
}: ManageInvitationProps) {
  const reason = useReason();
  const shown = invitation.status === "pending";
  const apply = (action: "deliver" | "reissue" | "revoke_invitation") => {
    const given = reason.take();
    if (given) onApply({ action, reason: given, invitation_id: invitation.id });
  };
  return (
    <Dialog
      isOpen
      onOpenChange={(open) => !open && onClose()}
      purpose="form"
      width={480}
    >
      <Layout
        header={
          <DialogHeader
            title={invitation.recipient_label}
            subtitle={invitationSummary(invitation)}
            onOpenChange={onClose}
          />
        }
        content={
          <LayoutContent>
            <ReasonField reason={reason} />
          </LayoutContent>
        }
        footer={
          <LayoutFooter>
            <HStack gap={2} hAlign="end" wrap="wrap">
              <Button
                label="Revoke invitation"
                variant="destructive"
                isDisabled={isPending}
                onClick={() => apply("revoke_invitation")}
              />
              <Button
                label={shown ? "Reissue link" : "Show signup link once"}
                variant="primary"
                isLoading={isPending}
                onClick={() => apply(shown ? "reissue" : "deliver")}
              />
            </HStack>
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}
