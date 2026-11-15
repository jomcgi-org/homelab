import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { useAnnounce } from "@astryxdesign/core/hooks";
import { ApiError } from "../api/client";
import { useLobby } from "../api/lobby";
import type { LobbyView } from "../api/schema";
import {
  platformKeys,
  useOperatorAccess,
  useOwnAccount,
  type PlatformUser,
} from "../api/platform";
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

function accessDenied(error: unknown): boolean {
  return error instanceof ApiError && [403, 404].includes(error.status);
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
  const [confirmed, setConfirmed] = useState<{
    viewer: LobbyView | undefined;
    account: PlatformUser | undefined;
    operator: WeakMap<object, { errors: number; error: unknown }> | null;
    changed: boolean;
  }>({
    viewer: undefined,
    account: undefined,
    operator: null,
    changed: false,
  });
  const cache = client.getQueryCache();
  const subscribe = useCallback(
    (notify: () => void) =>
      confirmed.changed ? () => {} : cache.subscribe(notify),
    [cache, confirmed.changed],
  );
  const knownDirectory = useSyncExternalStore(
    subscribe,
    () =>
      !confirmed.changed &&
      cache
        .findAll({ queryKey: platformKeys.directories })
        .some((query) => query.state.data !== undefined),
  );
  const directoryDenied = useSyncExternalStore(
    subscribe,
    () =>
      !confirmed.changed &&
      confirmed.operator !== null &&
      cache.findAll({ queryKey: platformKeys.directories }).some((query) => {
        const before = confirmed.operator?.get(query);
        return (
          accessDenied(query.state.error) &&
          (!before ||
            query.state.errorUpdateCount !== before.errors ||
            query.state.error !== before.error)
        );
      }),
  );
  const announce = useAnnounce();
  // Platform pages can be available without Grimoire permission. Once seen,
  // keep their own authoritative identity current even after leaving them.
  const account = useOwnAccount(
    Boolean(confirmed.account) && !confirmed.changed,
  );
  // A successful directory establishes operator access. Keep checking the
  // authoritative management endpoint after navigation, even without lobby access.
  const operator = useOperatorAccess(
    confirmed.operator !== null && !confirmed.changed,
  );
  const reloading = useRef(false);

  // Adjust this component's state before React renders its children. An
  // effect alone would let the old account's data render once more. Keep the
  // fence latched: clearing the lobby query must never reveal the old page.
  if (
    !confirmed.changed &&
    ((confirmed.viewer &&
      lobby.error instanceof ApiError &&
      lobby.error.status === 403) ||
      (confirmed.account && accessDenied(account.error)) ||
      (confirmed.operator && (accessDenied(operator.error) || directoryDenied)))
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
        : { ...confirmed, viewer: lobby.data },
    );
  } else if (
    account.data &&
    account.data !== confirmed.account &&
    !confirmed.changed
  ) {
    setConfirmed(
      confirmed.account && confirmed.account.id !== account.data.id
        ? { ...confirmed, changed: true }
        : { ...confirmed, account: account.data },
    );
  } else if (knownDirectory && !confirmed.operator && !confirmed.changed) {
    // Old refusals under other directory keys predate this access baseline.
    // Query identity also distinguishes a removed/recreated key; the error
    // identity covers a query reset whose update counter starts over.
    const baseline = new WeakMap(
      cache
        .findAll({ queryKey: platformKeys.directories })
        .map((query) => [
          query,
          { errors: query.state.errorUpdateCount, error: query.state.error },
        ]),
    );
    setConfirmed({ ...confirmed, operator: baseline });
  }

  useEffect(() => {
    if (!confirmed.changed || reloading.current) return;
    reloading.current = true;
    for (const campaign of confirmed.viewer?.campaigns ?? [])
      dropDraft(campaign.id);
    // Also drops the feedback draft and cancels reads still in flight.
    client.clear();
    document.title = "Grimoire";
    // Astryx keeps shared announcement regions outside the unmounted viewport.
    announce("", "assertive");
    announce("", "polite");
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
