import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Heading } from "@astryxdesign/core/Heading";
import { Item } from "@astryxdesign/core/Item";
import {
  HStack,
  Layout,
  LayoutContent,
  LayoutFooter,
  VStack,
} from "@astryxdesign/core/Layout";
import { List } from "@astryxdesign/core/List";
import type {
  Command,
  PlatformPermission,
  PlatformUser,
} from "../../api/platform";
import { ReasonField, useReason } from "./Reason";

interface ManageUserProps {
  user: PlatformUser;
  permissions: PlatformPermission[];
  isPending: boolean;
  onApply: (command: Command) => void;
  onClose: () => void;
}

/** Grant or revoke each permission, and enable or disable the account. */
export function ManageUser({
  user,
  permissions,
  isPending,
  onApply,
  onClose,
}: ManageUserProps) {
  const reason = useReason();
  const apply = (
    change:
      | { action: "set_active"; active: boolean }
      | { action: "grant" | "revoke_grant"; permission: string },
  ) => {
    const given = reason.take();
    if (given) onApply({ ...change, reason: given, user_id: user.id });
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
            title={user.username}
            subtitle={`${user.active ? "Active" : "Disabled"} · ${user.id}`}
            onOpenChange={onClose}
          />
        }
        content={
          <LayoutContent>
            <VStack gap={5}>
              <ReasonField reason={reason} />
              <VStack gap={2}>
                <Heading level={3}>Permissions</Heading>
                <List hasDividers>
                  {permissions.map((permission) => {
                    const granted = user.permissions.includes(permission.name);
                    return (
                      <Item
                        key={permission.name}
                        as="li"
                        label={permission.name}
                        description={permission.description}
                        descriptionLines={3}
                        endContent={
                          <Button
                            label={`${granted ? "Revoke" : "Grant"} ${permission.name}`}
                            size="sm"
                            isDisabled={isPending}
                            onClick={() =>
                              apply({
                                action: granted ? "revoke_grant" : "grant",
                                permission: permission.name,
                              })
                            }
                          >
                            {granted ? "Revoke" : "Grant"}
                          </Button>
                        }
                      />
                    );
                  })}
                </List>
              </VStack>
            </VStack>
          </LayoutContent>
        }
        footer={
          <LayoutFooter>
            <HStack gap={2} hAlign="end" wrap="wrap">
              <Button
                label={
                  user.active
                    ? "Disable platform access"
                    : "Enable platform account"
                }
                variant={user.active ? "destructive" : "secondary"}
                isDisabled={isPending}
                onClick={() =>
                  apply({ action: "set_active", active: !user.active })
                }
              />
            </HStack>
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}
