import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";

/** Character sheets: drafts, submission and the DM's approval. */
export function Sheets() {
  return (
    <Page title="Sheets" back={{ href: paths.lobby, label: "Your campaigns" }}>
      <EmptyState title="Not built yet" />
    </Page>
  );
}
