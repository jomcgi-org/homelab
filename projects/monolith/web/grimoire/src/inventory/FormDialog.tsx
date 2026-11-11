import type { ReactNode } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import {
  HStack,
  Layout,
  LayoutContent,
  LayoutFooter,
  VStack,
} from "@astryxdesign/core/Layout";

interface FormDialogProps {
  title: string;
  submitLabel: string;
  isPending: boolean;
  isInvalid: boolean;
  onSubmit: () => void;
  onClose: () => void;
  children: ReactNode;
}

/**
 * The shell every inventory write shares. It stays open until the caller
 * closes it, so a refused save keeps what was typed.
 */
export function FormDialog({
  title,
  submitLabel,
  isPending,
  isInvalid,
  onSubmit,
  onClose,
  children,
}: FormDialogProps) {
  return (
    <Dialog
      isOpen
      onOpenChange={(open) => !open && onClose()}
      purpose="form"
      width={480}
    >
      <Layout
        header={<DialogHeader title={title} onOpenChange={onClose} />}
        content={
          <LayoutContent>
            <VStack gap={4}>{children}</VStack>
          </LayoutContent>
        }
        footer={
          <LayoutFooter>
            <HStack gap={2} hAlign="end" wrap="wrap">
              <Button label="Cancel" variant="ghost" onClick={onClose} />
              <Button
                label={submitLabel}
                variant="primary"
                isLoading={isPending}
                isDisabled={isInvalid}
                onClick={onSubmit}
              />
            </HStack>
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}
