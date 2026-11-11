import { Drawer, DrawerHeader } from "@astryxdesign/core/Drawer";
import { Layout, LayoutContent, VStack } from "@astryxdesign/core/Layout";
import { Link } from "@astryxdesign/core/Link";
import { Text } from "@astryxdesign/core/Text";
import { useEntity } from "../api/knowledge";
import { paths } from "../app/paths";
import { fieldLabel, KnowledgeDetails } from "./KnowledgeDetails";

export interface KnowledgeDrawerProps {
  campaignId: string;
  /** The entity to show, or null when closed. */
  entityId: string | null;
  onClose: () => void;
}

/** A revoked grant must close this view soon after, so it re-reads while open. */
const RECHECK_MS = 4_000;

/** A side drawer with the viewer's projection of one entity. */
export function KnowledgeDrawer({
  campaignId,
  entityId,
  onClose,
}: KnowledgeDrawerProps) {
  const close = (open: boolean) => {
    if (!open) onClose();
  };
  return (
    <Drawer
      isOpen={entityId !== null}
      onOpenChange={close}
      label="Knowledge detail"
    >
      {/* Unmounted when closed so the entity is read afresh on every open. */}
      {entityId !== null && (
        <OpenEntity
          key={entityId}
          campaignId={campaignId}
          entityId={entityId}
          onOpenChange={close}
        />
      )}
    </Drawer>
  );
}

function OpenEntity({
  campaignId,
  entityId,
  onOpenChange,
}: {
  campaignId: string;
  entityId: string;
  onOpenChange: (open: boolean) => void;
}) {
  const query = useEntity(campaignId, entityId, RECHECK_MS);
  // Any failed read hides the entity, including one already on screen: the
  // usual cause is a grant retracted while the drawer was open.
  const entity = query.isError ? undefined : query.data;
  return (
    <Layout
      header={
        <DrawerHeader
          title={entity?.name ?? "Knowledge"}
          subtitle={entity && fieldLabel(entity.entity_type)}
          onOpenChange={onOpenChange}
        />
      }
      content={
        <LayoutContent>
          {entity ? (
            <VStack gap={4}>
              <KnowledgeDetails entity={entity} />
              <Link href={paths.entity(campaignId, entity.id)} isStandalone>
                Open knowledge page
              </Link>
            </VStack>
          ) : (
            <Text type="body" color="secondary">
              {query.isLoading
                ? "Loading knowledge…"
                : "This knowledge is no longer available to you."}
            </Text>
          )}
        </LayoutContent>
      }
    />
  );
}
