// Loaded only by Vite SSR: the complete report is HTML before any script runs.
import { renderToString } from "react-dom/server";
import { LeaderboardContent } from "./leaderboard-content";
import { leaderboard } from "./leaderboard-data";

export const html = renderToString(<LeaderboardContent data={leaderboard} />);
