// Both builds consume the exact snapshot that projects/model-bench writes.
// The owning package exports it through :leaderboard_sources for Bazel.
import snapshot from "../../../monolith/frontend/src/lib/public/llm-leaderboard/leaderboard.json";
import type { Leaderboard } from "../../../monolith/frontend/src/lib/public/llm-leaderboard/model.js";
export * from "../../../monolith/frontend/src/lib/public/llm-leaderboard/model.js";
export const leaderboard: Leaderboard = snapshot;
