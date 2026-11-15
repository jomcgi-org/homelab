import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { useClipboard } from "@astryxdesign/core/hooks";
import {
  HStack,
  Layout,
  LayoutContent,
  LayoutFooter,
  VStack,
} from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";

/** The gateway's registration page; a full page outside this app. */
const REGISTER = "/register";

/** The token sits in the fragment, so it never reaches a server log. */
export const signupLink = (token: string) =>
  `${location.origin}${REGISTER}#${token}`;

/**
 * The one time a signup link is shown. It lives only in this screen's memory,
 * so the dialog stays until the operator says they are done with it.
 */
export function SignupLink({
  link,
  onHide,
}: {
  link: string;
  onHide: () => void;
}) {
  const clipboard = useClipboard({ announce: "Copied" });
  return (
    <Dialog
      isOpen
      onOpenChange={(open) => !open && onHide()}
      purpose="required"
      width={480}
    >
      <Layout
        header={<DialogHeader title="Signup link" />}
        content={
          <LayoutContent>
            <VStack gap={3}>
              <TextInput
                label="Private signup link"
                value={link}
                isReadOnly
                width="100%"
              />
              <Text type="body" color="secondary">
                Copy and share privately. It is not shown again; a lost link
                needs a reissue.
              </Text>
            </VStack>
          </LayoutContent>
        }
        footer={
          <LayoutFooter>
            <HStack gap={2} hAlign="end" wrap="wrap">
              <Button label="Hide link" variant="ghost" onClick={onHide} />
              <Button
                label={clipboard.isCopied ? "Copied" : "Copy link"}
                variant="primary"
                onClick={() => clipboard.copy(link)}
              />
            </HStack>
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}
