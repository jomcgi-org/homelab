import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";

/** The viewer's journal across every session of a campaign. */
export function CampaignJournal() {
  return (
    <Page
      title="CampaignJournal"
      back={{ href: paths.lobby, label: "Your campaigns" }}
    >
      <EmptyState title="Not built yet" />
    </Page>
  );
}
