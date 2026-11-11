import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";

/** The DM's knowledge grants: who knows what, and how much. */
export function Grants() {
  return (
    <Page title="Grants" back={{ href: paths.lobby, label: "Your campaigns" }}>
      <EmptyState title="Not built yet" />
    </Page>
  );
}
