import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Page } from "../app/Page";
import { paths } from "../app/paths";

export function NotFound() {
  return (
    <Page title="Page not found">
      <EmptyState
        title="Nothing lives here"
        description="The link may be old, or the page may have moved."
        actions={<Button label="Your campaigns" href={paths.lobby} />}
      />
    </Page>
  );
}
