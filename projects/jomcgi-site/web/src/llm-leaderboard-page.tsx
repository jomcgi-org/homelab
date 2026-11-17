import { useEffect, useRef, useState } from "react";
import { Shell } from "./Shell";
import { onLeaderboardIntent } from "./leaderboard-intent";
import "./leaderboard.css";

// Vite removes the SSR branch from the browser bundle. Both data and chart
// code are loaded on intent; the initial client render preserves the HTML.
const serverHTML = import.meta.env.SSR
  ? (await import("./leaderboard-static")).html
  : undefined;

export function LlmLeaderboardPage() {
  const host = useRef<HTMLDivElement>(null);
  const [html] = useState(
    () =>
      serverHTML ??
      document.getElementById("leaderboard-report")?.innerHTML ??
      "",
  );
  useEffect(() => onLeaderboardIntent(host.current!), []);
  return (
    <Shell>
      <div
        id="leaderboard-report"
        ref={host}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </Shell>
  );
}
