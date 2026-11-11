import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";

/** One knowledge entry as this viewer may see it. */
export function Entity() {
  return (
    <Page title="Entity" back={{ href: paths.lobby, label: "Your campaigns" }}>
      <EmptyState title="Not built yet" />
    </Page>
  );
}
