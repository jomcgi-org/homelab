import { flushSync } from "react-dom";
import { hydrateRoot } from "react-dom/client";
import { LeaderboardContent } from "./leaderboard-content";
import { leaderboard } from "./leaderboard-data";

export function hydrate(host: HTMLElement) {
  let root: ReturnType<typeof hydrateRoot>;
  // Install handlers before replaying the first click or key that loaded us.
  flushSync(() => {
    root = hydrateRoot(host, <LeaderboardContent data={leaderboard} />);
  });
  return () => root.unmount();
}
