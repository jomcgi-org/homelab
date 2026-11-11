import { Button } from "@astryxdesign/core/Button";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { useOwnAccount, type PlatformUser } from "../../api/platform";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import { refusal, Unavailable } from "./Platform";

const GRIMOIRE_ACCESS = "grimoire.access";

/** The signed-in person's own platform account. */
export function Profile() {
  const account = useOwnAccount();
  return (
    <Page title="Your account">
      {refusal(account) === 404 ? (
        <Unavailable />
      ) : (
        <Loaded query={account} what="your account">
          {(user) => <Account user={user} />}
        </Loaded>
      )}
    </Page>
  );
}

function Account({ user }: { user: PlatformUser }) {
  return (
    <VStack gap={3} hAlign="start">
      <Text type="body">Welcome, {user.display_name}.</Text>
      <Text type="body">Your account is registered as {user.username}.</Text>
      {user.permissions.includes(GRIMOIRE_ACCESS) ? (
        <Button label="Open Grimoire" variant="primary" href={paths.lobby} />
      ) : (
        <>
          <Text type="body">
            Ask your administrator to enable Grimoire access for your account.
            Campaign invitations are separate.
          </Text>
          <Text type="body">Your platform user ID: {user.id}</Text>
        </>
      )}
    </VStack>
  );
}
