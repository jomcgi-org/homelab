import { useParams } from "react-router";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { ApiError, SignInError } from "../../api/client";
import { isEntityId, useEntity } from "../../api/knowledge";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import { fieldLabel, KnowledgeDetails } from "../../knowledge/KnowledgeDetails";

/** One knowledge entry as this viewer may see it. */
export function Entity() {
  const { campaignId = "", entityId = "" } = useParams();
  const known = isEntityId(entityId);
  const query = useEntity(campaignId, known ? entityId : null);
  const back = { href: paths.session(campaignId), label: "Back to the table" };
  // A refusal hides the entry even if an earlier read succeeded: the backend
  // answers a retracted or ungranted entry exactly like a missing one.
  const refused =
    query.error instanceof ApiError && !(query.error instanceof SignInError);
  const entity = refused ? undefined : query.data;

  return (
    <Page
      title={entity?.name ?? "Knowledge"}
      eyebrow={entity && fieldLabel(entity.entity_type)}
      back={back}
    >
      {!known || refused ? (
        <EmptyState
          title={
            known
              ? "This knowledge entry is not available to you."
              : "Knowledge entry not found."
          }
          actions={<Button label={back.label} href={back.href} />}
        />
      ) : (
        <Loaded query={query} what="this knowledge entry">
          {(loaded) => <KnowledgeDetails entity={loaded} />}
        </Loaded>
      )}
    </Page>
  );
}
