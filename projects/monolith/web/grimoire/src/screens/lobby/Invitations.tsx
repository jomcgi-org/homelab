import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Heading } from "@astryxdesign/core/Heading";
import { useClipboard } from "@astryxdesign/core/hooks";
import { Item } from "@astryxdesign/core/Item";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { List } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { IssuedLink, LobbyCampaignView } from "../../api/schema";
import {
  cancelInvitation,
  createJoinLink,
  invitePlayer,
  keys,
  revokeJoinLink,
  useInvitations,
  useJoinLinks,
} from "../../api/lobby";
import { gateway } from "../../app/paths";
import { useAction } from "../../app/useAction";

interface InvitationsProps {
  campaign: LobbyCampaignView;
  canAdministerAccounts: boolean;
  canEnroll: boolean;
}

const RECIPIENT = /^(?:[^\s@]+@[^\s@]+\.[^\s@]+|@[a-z0-9][a-z0-9_.-]{2,31})$/;

const utc = (value: string) =>
  `${new Date(value).toLocaleString("en-GB", { timeZone: "UTC" })} UTC`;

/**
 * The owner's invitations. Single-use links are offered when the backend
 * serves them; otherwise a registered player is invited by email.
 */
export function Invitations({
  campaign,
  canAdministerAccounts,
  canEnroll,
}: InvitationsProps) {
  const links = useJoinLinks(campaign.id);
  const invitations = useInvitations(campaign.id);
  const cancel = useAction(cancelInvitation, {
    invalidate: [keys.invitations(campaign.id)],
    success: "Invitation cancelled.",
  });
  return (
    <VStack gap={4}>
      <Heading level={4}>Invitations</Heading>
      {links.isError ? (
        <InviteByEmail campaignId={campaign.id} />
      ) : (
        <JoinLinks
          campaignId={campaign.id}
          links={links.data ?? []}
          canAdministerAccounts={canAdministerAccounts}
          canEnroll={canEnroll}
        />
      )}
      {(invitations.data?.length ?? 0) > 0 && (
        <List hasDividers header="Pending invitations">
          {invitations.data!.map((invite) => (
            <Item
              key={invite.id}
              as="li"
              label={invite.invitee_email}
              description="Pending"
              endContent={
                <Button
                  label="Cancel invitation"
                  size="sm"
                  variant="ghost"
                  isDisabled={cancel.isPending}
                  onClick={() =>
                    cancel.run({
                      campaignId: campaign.id,
                      invitationId: invite.id,
                    })
                  }
                />
              }
            />
          ))}
        </List>
      )}
    </VStack>
  );
}

function InviteByEmail({ campaignId }: { campaignId: string }) {
  const [email, setEmail] = useState("");
  const invite = useAction(invitePlayer, {
    invalidate: [keys.invitations(campaignId)],
    success: "Invitation sent. They get access after accepting.",
    onSuccess: () => setEmail(""),
  });
  const submit = () => {
    if (email.trim()) invite.run({ campaignId, email: email.trim() });
  };
  return (
    <HStack gap={2} vAlign="end" wrap="wrap">
      <TextInput
        label="Registered player's email or @username"
        description="They must have signed in to Grimoire once."
        value={email}
        onChange={(value) => setEmail(value.slice(0, 320))}
        onEnter={submit}
        autoComplete="off"
        width={320}
      />
      <Button
        label="Invite player"
        isLoading={invite.isPending}
        isDisabled={!email.trim()}
        onClick={submit}
      />
    </HStack>
  );
}

interface JoinLinksProps {
  campaignId: string;
  links: Array<Omit<IssuedLink, "token">>;
  canAdministerAccounts: boolean;
  canEnroll: boolean;
}

function JoinLinks({
  campaignId,
  links,
  canAdministerAccounts,
  canEnroll,
}: JoinLinksProps) {
  const [email, setEmail] = useState("");
  const [allowEnrollment, setAllowEnrollment] = useState(false);
  const [created, setCreated] = useState<IssuedLink | null>(null);
  const create = useAction(createJoinLink, {
    invalidate: [keys.joinLinks(campaignId)],
    onSuccess: (link) => {
      setCreated(link);
      setEmail("");
      setAllowEnrollment(false);
    },
  });
  const revoke = useAction(revokeJoinLink, {
    invalidate: [keys.joinLinks(campaignId)],
    success: "Link revoked.",
  });
  const valid = RECIPIENT.test(email.trim());
  const submit = () => {
    if (valid) create.run({ campaignId, email: email.trim(), allowEnrollment });
  };
  return (
    <VStack gap={4}>
      <VStack gap={2}>
        <TextInput
          label="Player's email or @username"
          description={
            canAdministerAccounts && !canEnroll
              ? "New-account invitations are unavailable. The player must already have signed in to Grimoire."
              : "Each link is for one player and can be accepted once."
          }
          value={email}
          onChange={(value) => setEmail(value.slice(0, 320))}
          onEnter={submit}
          autoComplete="off"
          width={320}
        />
        {canAdministerAccounts && canEnroll && (
          <CheckboxInput
            label="Allow this player to create a Grimoire account"
            value={allowEnrollment}
            onChange={setAllowEnrollment}
          />
        )}
        <HStack>
          <Button
            label="Create invitation link"
            isLoading={create.isPending}
            isDisabled={!valid}
            onClick={submit}
          />
        </HStack>
      </VStack>
      {created && (
        <CreatedLink link={created} onHide={() => setCreated(null)} />
      )}
      {links.length > 0 && (
        <List hasDividers header="Invitation links">
          {links.map((link) => (
            <Item
              key={link.id}
              as="li"
              label={link.invitee_email}
              description={`${link.status} · expires ${utc(link.expires_at)}`}
              endContent={
                (link.status === "pending" ||
                  link.enrollment_cleanup_pending) && (
                  <Button
                    label={
                      link.enrollment_cleanup_pending
                        ? "Retry cleanup"
                        : "Revoke link"
                    }
                    size="sm"
                    variant="ghost"
                    isDisabled={revoke.isPending}
                    onClick={() => revoke.run({ campaignId, linkId: link.id })}
                  />
                )
              }
            />
          ))}
        </List>
      )}
    </VStack>
  );
}

/** The full link is shown once. The token sits in the fragment, so it never reaches a server log. */
function CreatedLink({
  link,
  onHide,
}: {
  link: IssuedLink;
  onHide: () => void;
}) {
  const url = `${location.origin}${gateway.joinLanding}#${link.token}`;
  const clipboard = useClipboard();
  return (
    <VStack gap={2} role="status">
      <Text type="body">
        Link created for <strong>{link.invitee_email}</strong>. It expires{" "}
        {utc(link.expires_at)}. Copy it now; it is not shown again.
      </Text>
      <HStack gap={2} vAlign="end" wrap="wrap">
        <TextInput
          label="Private invitation link"
          value={url}
          isReadOnly
          width={420}
        />
        <Button
          label={clipboard.isCopied ? "Copied" : "Copy link"}
          variant="primary"
          onClick={() => clipboard.copy(url)}
        />
        <Button label="Hide link" variant="ghost" onClick={onHide} />
      </HStack>
    </VStack>
  );
}
