import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { useAnnounce } from "@astryxdesign/core/hooks";
import { ApiError } from "../api/client";
import { useLobby } from "../api/lobby";
import type { LobbyView } from "../api/schema";
import { dropDraft } from "../lib/session-expiry.js";

/** New memberships add access; changes to an existing seat invalidate its cache. */
function changedViewer(previous: LobbyView, current: LobbyView): boolean {
  if (previous.user.id !== current.user.id) return true;
  if (previous.can_administer_accounts && !current.can_administer_accounts)
    return true;
  return previous.campaigns.some((before) => {
    const after = current.campaigns.find((row) => row.id === before.id);
    return (
      !after ||
      before.role !== after.role ||
      before.player_character_id !== after.player_character_id ||
      before.is_owner !== after.is_owner
    );
  });
}

/**
 * Another tab can replace the sign-in cookie. Every screen shares cached
 * queries, so a confirmed viewer change removes the whole previous view.
 */
export function ViewerBoundary({ children }: { children: ReactNode }) {
  // App owns the read; this observer also wraps toasts without starting a
  // second request or querying when Providers renders a standalone component.
  const lobby = useLobby(false);
  const client = useQueryClient();
  const announce = useAnnounce();
  const [confirmed, setConfirmed] = useState<{
    viewer: LobbyView | undefined;
    changed: boolean;
  }>({ viewer: undefined, changed: false });
  const reloading = useRef(false);

  // Adjust this component's state before React renders its children. An
  // effect alone would let the old account's data render once more. Keep the
  // fence latched: clearing the lobby query must never reveal the old page.
  if (
    confirmed.viewer &&
    !confirmed.changed &&
    lobby.error instanceof ApiError &&
    lobby.error.status === 403
  ) {
    setConfirmed({ ...confirmed, changed: true });
  } else if (
    lobby.data &&
    lobby.data !== confirmed.viewer &&
    !confirmed.changed
  ) {
    setConfirmed(
      confirmed.viewer && changedViewer(confirmed.viewer, lobby.data)
        ? { ...confirmed, changed: true }
        : { viewer: lobby.data, changed: false },
    );
  }

  useEffect(() => {
    if (!confirmed.changed || reloading.current) return;
    reloading.current = true;
    for (const campaign of confirmed.viewer?.campaigns ?? [])
      dropDraft(campaign.id);
    // Also drops the feedback draft and cancels reads still in flight.
    client.clear();
    document.title = "Grimoire";
    announce("Your account or campaign access changed. Reloading Grimoire.");
    location.reload();
  }, [announce, client, confirmed]);

  if (confirmed.changed)
    return (
      <Banner
        status="warning"
        title="Your account or campaign access changed"
        description="Reloading Grimoire with your current access."
        endContent={<Button label="Reload" onClick={() => location.reload()} />}
        collapsible={false}
      />
    );
  return children;
}
