import { useEffect, useRef, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import {
  MetadataList,
  MetadataListItem,
} from "@astryxdesign/core/MetadataList";
import { Text } from "@astryxdesign/core/Text";
import { ApiError, errorMessage, SignInError } from "../../api/client";
import {
  acceptJoin,
  closeJoin,
  joinKeys,
  useJoinState,
  type JoinState,
} from "../../api/join";
import { keys } from "../../api/lobby";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { gateway, paths } from "../../app/paths";
import { useAction } from "../../app/useAction";

const expiry = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});

/** A signed-in player reviews a single-use campaign link and accepts it. */
export function JoinAccept() {
  const state = useJoinState();
  const client = useQueryClient();
  const navigate = useNavigate();
  // One request at a time across both buttons, even for two clicks that land
  // before the pending state has rendered.
  const busy = useRef(false);
  const once = async (run: () => Promise<unknown>) => {
    if (busy.current) return;
    busy.current = true;
    try {
      await run();
    } finally {
      busy.current = false;
    }
  };

  useEffect(() => {
    // A page restored from the back/forward cache still holds the invitation
    // it showed before. Drop it so only a fresh answer is ever displayed.
    const resume = (event: PageTransitionEvent) => {
      if (event.persisted) client.resetQueries({ queryKey: joinKeys.state });
    };
    window.addEventListener("pageshow", resume);
    return () => window.removeEventListener("pageshow", resume);
  }, [client]);

  const toLobby = () => navigate(paths.lobby);
  const accept = useAction(acceptJoin, {
    invalidate: [keys.lobby],
    success: () => `You joined ${state.data?.invitation.campaign_name}.`,
    onSuccess: toLobby,
  });
  const close = useAction(closeJoin, {
    invalidate: [keys.lobby],
    onSuccess: toLobby,
  });

  const onAccept = (invitationId: string) =>
    once(async () => {
      const joined = await accept.run(invitationId);
      // A refusal often means the invitation changed; show what it is now.
      if (!joined) await client.invalidateQueries({ queryKey: joinKeys.state });
    });

  const closeButton = (
    <Button
      label="Close"
      isLoading={close.isPending}
      isDisabled={accept.isPending}
      onClick={() => once(() => close.run(undefined))}
    />
  );

  // A refusal means there is no usable invitation in this browser, whatever
  // was on screen before. A dropped connection is left to Loaded's retry, and
  // an expired sign-in to the page's own notice.
  const refused =
    state.error instanceof ApiError && !(state.error instanceof SignInError);

  return (
    <Page title="Join a campaign" eyebrow="Grimoire" width={560}>
      {refused ? (
        <>
          <Banner
            status="error"
            title={errorMessage(state.error)}
            description="Open the original invitation again to continue."
            collapsible={false}
          />
          <HStack gap={2}>{closeButton}</HStack>
        </>
      ) : (
        <Loaded query={state} what="this invitation">
          {(data) => (
            <Invitation
              state={data}
              closeButton={closeButton}
              acceptButton={
                <Button
                  label="Accept invitation"
                  variant="primary"
                  isLoading={accept.isPending}
                  isDisabled={close.isPending}
                  onClick={() => onAccept(data.invitation.id)}
                />
              }
            />
          )}
        </Loaded>
      )}
    </Page>
  );
}

interface InvitationProps {
  state: JoinState;
  acceptButton: ReactNode;
  closeButton: ReactNode;
}

function Invitation({ state, acceptButton, closeButton }: InvitationProps) {
  const { invitation, email, matches } = state;
  const open = invitation.status === "pending";
  return (
    <VStack gap={4}>
      <Heading level={2}>{invitation.campaign_name}</Heading>
      <MetadataList label={{ position: "top" }}>
        <MetadataListItem label="Invited email">
          {invitation.invitee_email}
        </MetadataListItem>
        <MetadataListItem label="Signed in as">{email}</MetadataListItem>
        <MetadataListItem label="Expires">
          {expiry.format(new Date(invitation.expires_at))} UTC
        </MetadataListItem>
      </MetadataList>
      {open && !matches && (
        <Banner
          status="warning"
          title="The invited email differs from your account"
          description="Grimoire checks whether this is the account the owner invited when you accept. To use another account, sign out, then open the original invitation again."
          endContent={
            <Button label="Sign out" size="sm" href={gateway.signOut} />
          }
          collapsible={false}
        />
      )}
      <Text type="body">
        {open
          ? "You will join as a player. This link can be accepted once."
          : invitation.status === "accepted"
            ? "This invitation has already been accepted."
            : `This invitation is ${invitation.status}. Ask the campaign owner for a new link.`}
      </Text>
      <HStack gap={2} wrap="wrap">
        {open && acceptButton}
        {closeButton}
      </HStack>
    </VStack>
  );
}
