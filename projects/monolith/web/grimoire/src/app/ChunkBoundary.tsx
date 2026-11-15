import { Component, type ReactNode } from "react";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";

/**
 * Catches a screen that failed to load. The usual cause is a deploy: a tab
 * opened before it asks for a chunk the new build no longer has. A reload
 * fetches the current build.
 */
export class ChunkBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <EmptyState
        title="This page could not load"
        description="Grimoire may have been updated. Reload to get the current version."
        actions={<Button label="Reload" onClick={() => location.reload()} />}
      />
    );
  }
}
