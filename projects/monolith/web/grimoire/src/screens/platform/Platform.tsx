import { useState } from "react";
import { useSearchParams } from "react-router";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { ApiError, SignInError } from "../../api/client";
import { useDirectory } from "../../api/platform";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import { Accounts } from "./Accounts";
import { ReasonField, useReason } from "./Reason";
import { useCommand } from "./useCommand";

/** What a refused first load means: 404 is switched off, 403 needs bootstrap. */
export function refusal(query: {
  data: unknown;
  error: unknown;
}): number | null {
  const { data, error } = query;
  return data === undefined &&
    error instanceof ApiError &&
    !(error instanceof SignInError)
    ? error.status
    : null;
}

export function Unavailable() {
  return <EmptyState title="Account management is unavailable." />;
}

/** Account administration: users, permissions and invitations. */
export function Platform() {
  const [search] = useSearchParams();
  const selectedInvitation = search.get("invitation_id");
  const directory = useDirectory(selectedInvitation);
  const refused = refusal(directory);
  return (
    <Page
      title="Platform accounts"
      back={{ href: paths.lobby, label: "Back to Grimoire" }}
    >
      {refused === 404 ? (
        <Unavailable />
      ) : refused === 403 ? (
        <Bootstrap />
      ) : (
        <>
          <Text type="body" color="secondary">
            Platform signup and campaign invitations are separate. New accounts
            have no application access until you grant it.
          </Text>
          <Loaded query={directory} what="the accounts">
            {(data) => (
              <Accounts
                directory={data}
                selectedInvitation={selectedInvitation}
              />
            )}
          </Loaded>
        </>
      )}
    </Page>
  );
}

/**
 * Shown when management refuses the viewer. Importing works only for a signed
 * human operator; the backend rechecks that, so anyone else is refused again.
 */
function Bootstrap() {
  const [userId, setUserId] = useState("");
  const reason = useReason();
  const bootstrap = useCommand();
  const submit = () => {
    const given = reason.take();
    if (!given) return;
    const linked = userId.trim();
    bootstrap.run({
      action: "bootstrap",
      reason: given,
      ...(linked ? { user_id: linked } : {}),
    });
  };
  return (
    <VStack gap={3} hAlign="start">
      <Text type="body">
        An active platform operator account is required. Import your signed
        operator identity explicitly below.
      </Text>
      <TextInput
        label="Existing platform user ID"
        description="Only to link your second login issuer to an account you already have."
        isOptional
        value={userId}
        onChange={setUserId}
        width="100%"
      />
      <ReasonField reason={reason} />
      <Button
        label="Import my operator identity"
        variant="primary"
        isLoading={bootstrap.isPending}
        onClick={submit}
      />
    </VStack>
  );
}
