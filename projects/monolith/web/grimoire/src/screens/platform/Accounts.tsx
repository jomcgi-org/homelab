import { useState } from "react";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { Item } from "@astryxdesign/core/Item";
import { VStack } from "@astryxdesign/core/Layout";
import { List } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { Command, Directory } from "../../api/platform";
import {
  invitationSummary,
  isOpenInvitation,
  ManageInvitation,
} from "./ManageInvitation";
import { ManageUser } from "./ManageUser";
import { ReasonField, useReason } from "./Reason";
import { SignupLink, signupLink } from "./SignupLink";
import { useCommand } from "./useCommand";

const MAX_NAME = 100;

type Managing = { kind: "user" | "invitation"; id: string };

interface Confirmation {
  title: string;
  description: string;
  actionLabel: string;
  command: Command;
}

/** What a change that takes something away asks before it is sent. */
function confirmation(
  command: Command,
  directory: Directory,
): Confirmation | null {
  const user = (id: string) =>
    directory.users.items.find((row) => row.id === id)?.username ??
    "This account";
  const friend = (id: string) =>
    directory.invitations.items.find((row) => row.id === id)?.recipient_label ??
    "This friend";
  switch (command.action) {
    case "revoke_invitation":
      return {
        title: "Revoke this invitation?",
        description: `${friend(command.invitation_id)} can no longer use it to sign up.`,
        actionLabel: "Revoke invitation",
        command,
      };
    case "reissue":
      return {
        title: "Reissue this link?",
        description: `The link already shared with ${friend(command.invitation_id)} stops working.`,
        actionLabel: "Reissue link",
        command,
      };
    case "revoke_grant":
      return {
        title: "Revoke this permission?",
        description: `${user(command.user_id)} loses ${command.permission}.`,
        actionLabel: "Revoke permission",
        command,
      };
    case "set_active":
      return command.active
        ? null
        : {
            title: "Disable this account?",
            description: `${user(command.user_id)} loses platform access until the account is enabled again.`,
            actionLabel: "Disable platform access",
            command,
          };
    default:
      return null;
  }
}

interface AccountsProps {
  directory: Directory;
  /** The invitation a delivery reference opened, from `?invitation_id=`. */
  selectedInvitation: string | null;
}

/** Invitations and users, each managed from its own row. */
export function Accounts({ directory, selectedInvitation }: AccountsProps) {
  const users = directory.users.items;
  const invitations = directory.invitations.items;
  const [managing, setManaging] = useState<Managing | null>(() =>
    invitations.some(
      (row) => row.id === selectedInvitation && isOpenInvitation(row),
    )
      ? { kind: "invitation", id: selectedInvitation! }
      : null,
  );
  const [confirming, setConfirming] = useState<Confirmation | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const command = useCommand((result, sent) => {
    setConfirming(null);
    // Granting leaves the account open for its next permission.
    if (sent.action !== "grant") setManaging(null);
    if ("token" in result) setLink(signupLink(result.token));
  });
  const apply = (change: Command) => {
    const confirm = confirmation(change, directory);
    if (!confirm) return void command.run(change);
    // One dialog at a time: the confirmation replaces the manage dialog.
    setManaging(null);
    setConfirming(confirm);
  };
  const managedUser =
    managing?.kind === "user" && users.find((row) => row.id === managing.id);
  const managedInvitation =
    managing?.kind === "invitation" &&
    invitations.find((row) => row.id === managing.id);
  return (
    <>
      {directory.selectedMissing && (
        <Banner
          status="warning"
          title="Invitation not found"
          description="The invitation this link names does not exist."
          collapsible={false}
        />
      )}
      <InviteFriend />
      <VStack gap={3} as="section" aria-labelledby="invitations-heading">
        <Heading level={2} id="invitations-heading">
          Invitations
        </Heading>
        {invitations.length === 0 ? (
          <EmptyState isCompact headingLevel={3} title="No invitations yet" />
        ) : (
          <List hasDividers>
            {invitations.map((invitation) => (
              <Item
                key={invitation.id}
                as="li"
                label={invitation.recipient_label}
                labelLines={2}
                description={invitationSummary(invitation)}
                descriptionLines={2}
                endContent={
                  isOpenInvitation(invitation) && (
                    <Button
                      label={`Manage invitation for ${invitation.recipient_label}`}
                      size="sm"
                      onClick={() =>
                        setManaging({ kind: "invitation", id: invitation.id })
                      }
                    >
                      Manage
                    </Button>
                  )
                }
              />
            ))}
          </List>
        )}
      </VStack>
      <VStack gap={3} as="section" aria-labelledby="users-heading">
        <Heading level={2} id="users-heading">
          Users
        </Heading>
        <List hasDividers>
          {users.map((user) => (
            <Item
              key={user.id}
              as="li"
              label={user.username}
              labelLines={2}
              description={`${user.active ? "Active" : "Disabled"} · Grants: ${
                user.permissions.join(", ") || "None"
              }`}
              descriptionLines={3}
              endContent={
                <Button
                  label={`Manage ${user.username}`}
                  size="sm"
                  onClick={() => setManaging({ kind: "user", id: user.id })}
                >
                  Manage
                </Button>
              }
            />
          ))}
        </List>
      </VStack>
      {(directory.users.next || directory.invitations.next) && (
        <Text type="supporting" color="secondary">
          This page shows the first 50 entries. Use the paginated management API
          or MCP for more.
        </Text>
      )}
      {managedUser && (
        <ManageUser
          user={managedUser}
          permissions={directory.permissions}
          isPending={command.isPending}
          onApply={apply}
          onClose={() => setManaging(null)}
        />
      )}
      {managedInvitation && (
        <ManageInvitation
          invitation={managedInvitation}
          isPending={command.isPending}
          onApply={apply}
          onClose={() => setManaging(null)}
        />
      )}
      <AlertDialog
        isOpen={confirming !== null}
        onOpenChange={(open) => !open && setConfirming(null)}
        title={confirming?.title ?? ""}
        description={confirming?.description ?? ""}
        actionLabel={confirming?.actionLabel ?? ""}
        isActionLoading={command.isPending}
        onAction={() => confirming && command.run(confirming.command)}
      />
      {link && <SignupLink link={link} onHide={() => setLink(null)} />}
    </>
  );
}

/** Prepares an invitation; its signup link is shown later, once, from its row. */
function InviteFriend() {
  const [name, setName] = useState("");
  const [isNameMissing, setNameMissing] = useState(false);
  const reason = useReason();
  const issue = useCommand(() => {
    setName("");
    reason.set("");
  });
  const submit = () => {
    const given = reason.take();
    const friend = name.trim();
    setNameMissing(!friend);
    if (given && friend)
      issue.run({ action: "issue", reason: given, recipient_label: friend });
  };
  return (
    <VStack
      gap={3}
      hAlign="start"
      as="section"
      aria-labelledby="invite-heading"
    >
      <Heading level={2} id="invite-heading">
        Invite a friend
      </Heading>
      <TextInput
        label="Friend's name"
        isRequired
        value={name}
        onChange={(value) => {
          setName(value.slice(0, MAX_NAME));
          setNameMissing(false);
        }}
        status={
          isNameMissing
            ? { type: "error", message: "Name the friend first." }
            : undefined
        }
        width="100%"
      />
      <ReasonField reason={reason} />
      <Button
        label="Prepare seven-day invitation"
        variant="primary"
        isLoading={issue.isPending}
        onClick={submit}
      />
    </VStack>
  );
}
