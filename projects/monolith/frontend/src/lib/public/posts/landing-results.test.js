import { expect, test } from "vitest";
import agent from "./agent-replay.json";
import research from "./qwen-replay.json";
import { landingResults } from "./landing-results.js";

test("server-rendered landing results match the recordings", () => {
  const recordings = { research, coding: agent };
  for (const row of landingResults) {
    const { metrics, usage } = recordings[row.kind].turns[0];
    expect(row.inputTokens).toBe(usage.prompt_tokens);
    expect(row.firstTokenSeconds).toBe(
      Number((metrics.ttftMs / 1000).toFixed(2)),
    );
    expect(row.decodeRate).toBe(Number(metrics.tokensPerSecond.toFixed(1)));
  }
});
