// Keep the pre-rendered report readable while its code/data chunk is absent.
// Pointer/focus starts hydration early; a click or key during the download is
// replayed once handlers exist, so the first interaction is never discarded.
export function onLeaderboardIntent(
  host: HTMLElement,
  load = () => import("./leaderboard-interactive"),
) {
  let pending: Promise<void> | undefined;
  let hydrated = false;
  let disposed = false;
  let unmount: (() => void) | undefined;
  const activate = () => {
    pending ??= load()
      .then(({ hydrate }) => {
        if (disposed) return;
        unmount = hydrate(host);
        hydrated = true;
      })
      .catch(() => {
        // Keep the report intact and allow the next intent to retry.
        pending = undefined;
      });
    return pending;
  };
  const warm = () => {
    void activate();
  };
  const action = (event: Event) => {
    if (hydrated || !(event.target instanceof HTMLElement)) return;
    const target = event.target;
    // Links and document text keep their native behaviour even if loading fails.
    if (event.type === "click" && !target.closest("button, input, summary"))
      return;
    // Preserve native typing, tab navigation and button keyboard activation.
    // Only the search input has a key handler that needs replaying.
    if (
      event instanceof KeyboardEvent &&
      (!(target instanceof HTMLInputElement) ||
        target.type !== "search" ||
        !["Enter", "Escape"].includes(event.key))
    )
      return;
    const value =
      event.type === "input" && target instanceof HTMLInputElement
        ? target.value
        : undefined;
    event.preventDefault();
    event.stopImmediatePropagation();
    const replay =
      event instanceof KeyboardEvent
        ? new KeyboardEvent(event.type, event)
        : event instanceof MouseEvent
          ? new MouseEvent(event.type, event)
          : new Event(event.type, { bubbles: true, cancelable: true });
    void activate().then(() => {
      if (!hydrated || disposed || !host.contains(target)) return;
      if (value !== undefined && target instanceof HTMLInputElement) {
        // Hydration attaches React's value tracker to an already edited input.
        // Restore each queued edit through the native setter so onChange sees it.
        target.value = "";
        Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype,
          "value",
        )!.set!.call(target, value);
      }
      target.dispatchEvent(replay);
    });
  };
  const linked = () => {
    if (new URL(window.location.href).searchParams.has("m")) warm();
  };
  const warmEvents = ["pointerover", "focusin", "touchstart"];
  const actionEvents = ["click", "keydown", "input", "change"];
  warmEvents.forEach((type) =>
    host.addEventListener(type, warm, { passive: true }),
  );
  actionEvents.forEach((type) => host.addEventListener(type, action, true));
  window.addEventListener("popstate", linked);
  linked();
  return () => {
    disposed = true;
    warmEvents.forEach((type) => host.removeEventListener(type, warm));
    actionEvents.forEach((type) =>
      host.removeEventListener(type, action, true),
    );
    window.removeEventListener("popstate", linked);
    // The outer React root may be unmounting; do not unmount a nested root
    // synchronously during that render.
    if (unmount) queueMicrotask(unmount);
  };
}
