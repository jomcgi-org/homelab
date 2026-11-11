import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";

/** Account administration: users, permissions and invitations. */
export function Platform() {
  return (
    <Page
      title="Platform"
      back={{ href: paths.lobby, label: "Your campaigns" }}
    >
      <EmptyState title="Not built yet" />
    </Page>
  );
}
