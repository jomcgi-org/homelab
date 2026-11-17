// One snapshot, still written by projects/model-bench and owned by the hub.
import snapshot from "../../../monolith/frontend/src/lib/public/llm-leaderboard/leaderboard.json";
import type { Leaderboard } from "./leaderboard-model";
export const leaderboard: Leaderboard = snapshot;
