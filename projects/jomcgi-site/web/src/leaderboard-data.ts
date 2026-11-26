// Frozen historical model-bench snapshot.
import snapshot from "../hub/src/lib/public/llm-leaderboard/leaderboard.json";
import type { Leaderboard } from "./leaderboard-model";
export const leaderboard: Leaderboard = snapshot;
