import { RUN_FIXTURES } from "../src/routes/private/factory/execution/run-fixtures.js";

export const NOW = "2026-10-03T12:00:00.000Z";
export const session = {
  id: 901,
  title: "Synthetic running session",
  status: "running",
  model: "luna",
  repo: "fixture/synthetic",
  workflow_id: "fixture-running",
  local_session_id: "synthetic-session",
  ember_session_id: "synthetic-vm",
  created_at: NOW,
  last_turn_at: NOW,
  total_cost_usd: 0.02,
};
export const executionData = {
  sessions: [session],
  models: [{ id: "luna", name: "Luna", family: "codex" }],
  sessionStopControlEnabled: false,
  error: false,
};

export function createExecutionFixture() {
  let revision = 0;
  const streams = new Set();
  const stats = {
    calls: {},
    unexpected: [],
    created: 0,
    closed: 0,
    active: 0,
    maxActive: 0,
  };
  const run = structuredClone(RUN_FIXTURES.running.run);
  run.title = "Synthetic running task";
  run.task.text = "Synthetic running task";
  run.created_at = NOW;
  run.updated_at = NOW;
  run.deviations = [];
  run.cost_usd = 0.02;
  run.plan.budget_usd = 1;
  run.nodes[0].attempts = [
    {
      ...run.nodes[0].attempts.at(-1),
      n: 1,
      session_id: session.id,
      started_at: NOW,
      ended_at: null,
      finding: null,
      rationale: null,
      events: [],
    },
  ];
  const job = () => ({
    name: "synthetic-drain",
    state: "running",
    outcome: null,
    prompt_head: "Synthetic drain item",
    prompt: "Synthetic drain item",
    summary_head: "",
    last_summary: "Synthetic detail only.",
    locked_at: NOW,
    last_run_at: NOW,
    next_run_at: NOW,
    session: {
      ...session,
      voice_summary: `Synthetic drain update ${revision}`,
      activities: [],
      tool_calls: revision + 1,
    },
  });
  const vms = () => ({
    [session.ember_session_id]: {
      state: revision % 2 ? "asleep" : "awake",
      cp_state: "running",
    },
  });
  const response = (body) =>
    new Response(JSON.stringify(body), {
      headers: { "content-type": "application/json" },
    });
  const fetch = async (input, options = {}) => {
    const url = new URL(String(input), "https://private.fixture.invalid");
    const path = url.pathname;
    stats.calls[path] = (stats.calls[path] || 0) + 1;
    if (
      (options.method || "GET") !== "GET" ||
      url.origin !== "https://private.fixture.invalid"
    ) {
      stats.unexpected.push(`${options.method || "GET"} ${input}`);
      throw new Error(`Fixture refuses mutation or external request: ${input}`);
    }
    if (path === "/factory/execution/data")
      return response({ sessions: [session] });
    if (path === "/factory/execution/runs")
      return response({
        runs: url.searchParams.get("active") === "false" ? [] : [run],
      });
    if (path === "/factory/execution/runs/fixture-running")
      return response({
        run: { ...run, note: `Synthetic run update ${revision}` },
        sessions: [session],
        view: { engine_tier: "live", now: NOW, snapshot_age_seconds: 0 },
      });
    if (path === "/factory/execution/session/901")
      return response({
        session,
        pending_queue: [],
        turns: [
          {
            seq: 1,
            prompt: "Inspect the synthetic workspace",
            model: "luna",
            created_at: NOW,
            result_text:
              `Synthetic transcript update ${revision}\n\n` +
              "Synthetic observation.\n\n".repeat(60),
            terminal_reason: "completed",
            rationale: {
              parse_status: "parsed",
              paths: [{ path: "synthetic.js", why: "Fixture walkthrough" }],
              deviations: [],
            },
          },
        ],
      });
    if (path === "/factory/execution/vms") return response({ vms: vms() });
    if (path === "/api/swarm/walkthrough/901/1")
      return response({
        rung: 1,
        ephemeral: false,
        summary: {
          status: "available",
          files_changed: 2,
          insertions: 2,
          deletions: 0,
          accounted_files: 2,
        },
        steps: ["synthetic.js", "selection.js"].map((path) => ({
          type: "authored",
          register: "testimony",
          file_path: path,
          file_change: { additions: 1, deletions: 0, status: "modified" },
          testimony: {
            turn: 1,
            attempt: 1,
            points: [{ path, why: `Fixture walkthrough for ${path}` }],
          },
        })),
      });
    if (path === "/api/swarm/compare/901/1/patch")
      return response({ patch: "@@ -1 +1 @@\n-before\n+after" });
    if (path === "/factory/execution/repos") return response({ repos: [] });
    if (path === "/factory")
      return response({
        active: [],
        queued: [],
        escalated: [],
        control: { enabled: true },
      });
    if (path === "/factory/execution/drain-lane")
      return response({ state: "running", active: 1 });
    if (path === "/factory/execution/drain/console")
      return response({
        now: NOW,
        lane: {
          state: "running",
          reap_after_seconds: 900,
          cycle: {
            workflow_id: "synthetic-cycle",
            status: "PENDING",
            created_at: NOW,
            last_checkpoint_at: NOW,
            last_step: `Synthetic checkpoint ${revision}`,
            claims: 1,
            finishes: 0,
          },
        },
        queue: {
          running: 1,
          due: 0,
          scheduled: 0,
          deferred: 0,
          error: 0,
          ok: 1,
          parked: 0,
        },
        jobs: [job(), completedJob()],
        recent_cycles: [],
      });
    if (path === "/factory/execution/drain/jobs/synthetic-drain")
      return response({ now: NOW, job: job(), attempts: [] });
    if (path === "/factory/execution/drain/jobs/synthetic-completed")
      return response({ now: NOW, job: completedJob(), attempts: [] });
    stats.unexpected.push(String(input));
    throw new Error(`Unexpected execution fixture endpoint: ${input}`);
  };
  function completedJob() {
    return {
      ...job(),
      name: "synthetic-completed",
      state: "ok",
      prompt: "Synthetic completed drain item",
      prompt_head: "Synthetic completed drain item",
      locked_at: null,
      next_run_at: null,
      session: null,
    };
  }
  class EventSource {
    static OPEN = 1;
    static CLOSED = 2;
    readyState = 1;
    constructor(url) {
      if (url !== "/private/factory/execution/vms/stream") {
        stats.unexpected.push(url);
        throw new Error(`Unexpected fixture stream: ${url}`);
      }
      streams.add(this);
      stats.created++;
      stats.active = streams.size;
      stats.maxActive = Math.max(stats.maxActive, stats.active);
      queueMicrotask(() => {
        if (streams.has(this)) {
          this.onopen?.();
          this.frame();
        }
      });
    }
    frame() {
      this.onmessage?.({ data: JSON.stringify({ vms: vms() }) });
    }
    close() {
      if (streams.delete(this)) stats.closed++;
      this.readyState = 2;
      stats.active = streams.size;
    }
  }
  return {
    fetch,
    EventSource,
    stats,
    advance() {
      revision++;
      for (const stream of streams) stream.frame();
      return revision;
    },
    disconnect() {
      for (const stream of streams) {
        stream.readyState = 0;
        for (let i = 0; i < 3; i++) stream.onerror?.();
      }
    },
    reconnect() {
      for (const stream of streams) {
        stream.readyState = 1;
        stream.onopen?.();
        stream.frame();
      }
    },
  };
}
