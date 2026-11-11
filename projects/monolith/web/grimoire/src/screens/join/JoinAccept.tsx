import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";

/** A signed-in player reviews a single-use campaign link and accepts it. */
export function JoinAccept() {
  return (
    <Page
      title="JoinAccept"
      back={{ href: paths.lobby, label: "Your campaigns" }}
    >
      <EmptyState title="Not built yet" />
    </Page>
  );
}
