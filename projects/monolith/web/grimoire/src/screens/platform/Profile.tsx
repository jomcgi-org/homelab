import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";

/** The signed-in person's own platform account. */
export function Profile() {
  return (
    <Page title="Profile" back={{ href: paths.lobby, label: "Your campaigns" }}>
      <EmptyState title="Not built yet" />
    </Page>
  );
}
