import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Item } from "@astryxdesign/core/Item";
import { List } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { InvitationView, LobbyView } from "../../api/schema";
import {
  createCampaign,
  decideInvitation,
  keys,
  useLobby,
} from "../../api/lobby";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { gateway, paths } from "../../app/paths";
import { useAction } from "../../app/useAction";
import { CampaignCard } from "./CampaignCard";

const inboxLabel = (count: number) =>
  count > 0 ? `Feedback inbox (${count})` : "Feedback inbox";

/** The signed-in home: invitations, campaigns, and starting a new one. */
export function Lobby() {
  const lobby = useLobby();
  return (
    <Page
      title="Your campaigns"
      eyebrow="Grimoire"
      actions={
        <>
          <Button label="Character sheets" href={paths.sheets()} />
          {lobby.data?.can_administer_accounts && (
            <>
              <Button label="Accounts" href={paths.platform} />
              <Button
                label={inboxLabel(lobby.data.new_feedback_count ?? 0)}
                href={paths.feedback}
              />
            </>
          )}
          <Button label="Sign out" variant="ghost" href={gateway.signOut} />
        </>
      }
    >
      <Loaded query={lobby} what="your campaigns">
        {(data) => <LobbyContent lobby={data} />}
      </Loaded>
    </Page>
  );
}

function LobbyContent({ lobby }: { lobby: LobbyView }) {
  return (
    <>
      <Text type="body" color="secondary">
        Welcome, {lobby.user.display_name || lobby.user.email}.
      </Text>
      {lobby.invitations.length > 0 && (
        <Invitations invitations={lobby.invitations} />
      )}
      <VStack gap={3} as="section" aria-labelledby="campaigns-heading">
        <Heading level={2} id="campaigns-heading">
          Campaigns
        </Heading>
        {lobby.campaigns.length === 0 ? (
          <EmptyState
            isCompact
            headingLevel={3}
            title="No campaigns yet"
            description={`Start your own below, or ask an owner to invite ${lobby.user.email}.`}
          />
        ) : (
          lobby.campaigns.map((campaign) => (
            <CampaignCard
              key={campaign.id}
              campaign={campaign}
              canAdministerAccounts={lobby.can_administer_accounts}
              canEnroll={lobby.invitation_enrollment_enabled === true}
            />
          ))
        )}
      </VStack>
      {lobby.can_create_game !== false && <StartCampaign />}
    </>
  );
}

function Invitations({ invitations }: { invitations: InvitationView[] }) {
  const decide = useAction(decideInvitation, {
    invalidate: [keys.lobby],
    success: (_result, input) =>
      input.decision === "accept"
        ? "Invitation accepted."
        : "Invitation declined.",
  });
  return (
    <VStack gap={3} as="section" aria-labelledby="invitations-heading">
      <Heading level={2} id="invitations-heading">
        Invitations
      </Heading>
      <List hasDividers>
        {invitations.map((invite) => (
          <Item
            key={invite.id}
            as="li"
            label={invite.campaign_name}
            labelLines={2}
            description="Join this campaign as a player."
            endContent={
              <HStack gap={2}>
                <Button
                  label="Accept"
                  variant="primary"
                  size="sm"
                  isDisabled={decide.isPending}
                  onClick={() =>
                    decide.run({ invitationId: invite.id, decision: "accept" })
                  }
                />
                <Button
                  label="Decline"
                  size="sm"
                  isDisabled={decide.isPending}
                  onClick={() =>
                    decide.run({ invitationId: invite.id, decision: "decline" })
                  }
                />
              </HStack>
            }
          />
        ))}
      </List>
    </VStack>
  );
}

function StartCampaign() {
  const [name, setName] = useState("");
  const create = useAction(createCampaign, {
    invalidate: [keys.lobby],
    success: "Campaign created. You are its owner and first DM.",
    onSuccess: () => setName(""),
  });
  const submit = () => {
    if (name.trim()) create.run(name.trim());
  };
  return (
    <VStack gap={3} as="section" aria-labelledby="create-heading">
      <Heading level={2} id="create-heading">
        Start a campaign
      </Heading>
      <HStack gap={2} vAlign="end" wrap="wrap">
        <TextInput
          label="Campaign name"
          description="You will be its owner and first DM."
          placeholder="The next adventure"
          value={name}
          onChange={(value) => setName(value.slice(0, 120))}
          onEnter={submit}
          width={320}
        />
        <Button
          label="Create campaign"
          variant="primary"
          isLoading={create.isPending}
          isDisabled={!name.trim()}
          onClick={submit}
        />
      </HStack>
    </VStack>
  );
}
