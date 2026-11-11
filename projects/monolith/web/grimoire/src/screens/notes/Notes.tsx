import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";

/** Campaign notes outside a session: character and party notes. */
export function Notes() {
  return (
    <Page title="Notes" back={{ href: paths.lobby, label: "Your campaigns" }}>
      <EmptyState title="Not built yet" />
    </Page>
  );
}
