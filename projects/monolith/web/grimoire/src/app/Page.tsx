import type { ReactNode } from "react";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import {
  HStack,
  Layout,
  LayoutContent,
  LayoutHeader,
  StackItem,
  VStack,
} from "@astryxdesign/core/Layout";
import { Link } from "@astryxdesign/core/Link";
import { Text } from "@astryxdesign/core/Text";
import { useEffect } from "react";
import { useSignIn } from "./useSignIn";

interface PageProps {
  /** Document title and the page's h1. */
  title: string;
  /** Small label above the title: the viewer's role or the section. */
  eyebrow?: string;
  /** Where the back link goes. Omit on the lobby. */
  back?: { href: string; label: string };
  /** Header actions, end-aligned. */
  actions?: ReactNode;
  /** Content width in pixels. Reading pages are narrow; the table is wide. */
  width?: number;
  children: ReactNode;
}

/**
 * The frame every screen renders inside: back link, eyebrow, title, actions,
 * and the sign-in notice. Screens supply only their content.
 */
export function Page({
  title,
  eyebrow,
  back,
  actions,
  width = 860,
  children,
}: PageProps) {
  useEffect(() => {
    document.title = `${title} · Grimoire`;
  }, [title]);
  return (
    <Layout
      height="auto"
      contentWidth={width}
      header={
        <LayoutHeader hasDivider>
          <HStack gap={4} vAlign="end" wrap="wrap">
            <StackItem size="fill">
              <VStack gap={1}>
                {back && (
                  <Link href={back.href} isStandalone size="sm">
                    {back.label}
                  </Link>
                )}
                {eyebrow && (
                  <Text type="label" color="secondary">
                    {eyebrow}
                  </Text>
                )}
                <Heading level={1}>{title}</Heading>
              </VStack>
            </StackItem>
            {actions && (
              <HStack gap={2} wrap="wrap" hAlign="end">
                {actions}
              </HStack>
            )}
          </HStack>
        </LayoutHeader>
      }
      content={
        <LayoutContent>
          <VStack gap={6}>
            <SignInNotice />
            {children}
          </VStack>
        </LayoutContent>
      }
    />
  );
}

/** Shown only once an expired sign-in is confirmed and the tab cannot reload itself. */
function SignInNotice() {
  const state = useSignIn();
  if (state !== "signed-out") return null;
  return (
    <Banner
      status="warning"
      title="Signed out"
      description="Reload to sign in again. Copy anything unsaved on this page first."
      endContent={
        <Button label="Reload" size="sm" onClick={() => location.reload()} />
      }
      collapsible={false}
    />
  );
}
