const NOW = "2026-08-10T23:00:00Z";

const plan = {
  pinned: true,
  source: "recorded",
  max_attempts: 2,
  implementer_model: "luna",
  reviewer_model: "opus",
  turn_timeout_seconds: 1800,
  version: 1,
  budget_usd: null,
};

function attempt(n, state, extra = {}) {
  return {
    n,
    session_id: 170 + n,
    local_session_id: `fixture-implement-${n}`,
    model: "luna",
    state,
    started_at: "2026-08-10T22:40:00Z",
    ended_at: state === "running" ? null : "2026-08-10T22:48:00Z",
    cost_usd: n === 1 ? 0.08 : 0.12,
    finding: null,
    live: null,
    ...extra,
  };
}

function node(key, label, state, overrides = {}) {
  return {
    key,
    kind: key === "push_gate" ? "gate" : "work",
    label,
    state,
    model: key === "push_gate" ? null : key === "review" ? "opus" : "luna",
    attempts: [],
    queue: null,
    decision: null,
    verdict: null,
    note: null,
    deps:
      key === "implement"
        ? []
        : key === "push_gate"
          ? ["implement"]
          : ["push_gate"],
    blocked_on: null,
    evidence: null,
    ...overrides,
  };
}

function run(name, state, overrides = {}) {
  return {
    workflow_id: `fixture-${name}`,
    dbos_status:
      state === "queued"
        ? "ENQUEUED"
        : state === "cancelled"
          ? "CANCELLED"
          : "PENDING",
    state,
    task: {
      text: `Fixture ${name} run`,
      repo: "jomcgi/homelab",
      base_branch: "main",
    },
    work_branch: `claude/fixture-${name}`,
    branch_url: `https://github.com/jomcgi/homelab/tree/claude/fixture-${name}`,
    created_at: "2026-08-10T22:40:00Z",
    updated_at: NOW,
    completed_at:
      state === "running" || state === "queued" || state === "stranded"
        ? null
        : NOW,
    app_version: "b7a44d1c",
    server_app_version: "b7a44d1c",
    stranded: state === "stranded",
    cost_usd: 0.2,
    note: null,
    plan,
    nodes: [
      node("implement", "implement", "future"),
      node("push_gate", "push gate", "future"),
      node("review", "review", "future"),
    ],
    cancelled_by: state === "cancelled" ? { actor: "joe", at: NOW } : null,
    ...overrides,
  };
}

function entry(runValue, sessions = []) {
  return {
    run: runValue,
    view: { engine_tier: "live", now: NOW, snapshot_age_seconds: 0 },
    sessions,
  };
}

function homeEntry(name, runs, sessions, view = {}) {
  return {
    home: true,
    master: { runs, queues: [] },
    activity: [
      ...runs.map((value) => ({ kind: "run", value, cost: value.cost_usd })),
      ...sessions.map((value) => ({
        kind: "session",
        value,
        cost: value.total_cost_usd,
      })),
    ],
    sessions,
    view: { engine_tier: "live", snapshot_age_seconds: 0, ...view },
    name,
  };
}

const homeSession = (id, title, at, extra = {}) => ({
  id,
  title,
  local_session_id: `fixture-home-${id}`,
  status: "completed",
  model: "luna",
  created_at: at,
  last_turn_at: at,
  total_cost_usd: 0.06,
  ...extra,
});

const homeActivityRun = run("home-activity", "approved", {
  title: "summarize the past week's work",
  created_at: "2026-08-03T22:40:00Z",
  updated_at: "2026-08-10T22:53:00Z",
  completed_at: "2026-08-10T22:53:00Z",
  cost_usd: 0.29,
});

const homeAttentionRun = run("home-attention", "escalated", {
  title: "review the deployment plan",
  needs: { reason: "branch head needs a decision" },
});

const running = run("running", "running", {
  plan: { ...plan, budget_usd: 0.15 },
  deviations: [
    {
      code: "retry_taken",
      node_key: "implement",
      evidence: "attempts: 2; retry finding code: head_unchanged",
      text: "implement took 2 attempts after head_unchanged.",
    },
    {
      code: "budget_exceeded",
      node_key: "run",
      evidence: "cost_usd: $0.20; pinned budget_usd: $0.15",
      text: "run spent $0.20 against a pinned $0.15 budget.",
    },
  ],
  nodes: [
    node("implement", "implement", "running", {
      attempts: [
        attempt(1, "gated", {
          finding: {
            code: "head_unchanged",
            text: "the branch head did not move during the turn",
            observed_head: "86dcbf41",
          },
          rationale: {
            raw: "RATIONALE\n- path: swarm/rows.py · why: carries the final turn\n- path: swarm/run_view.py · why: shows testimony\n- deviation: kept routing unchanged",
            parse_status: "parsed",
            paths: [
              { path: "swarm/rows.py", why: "carries the final turn" },
              { path: "swarm/run_view.py", why: "shows testimony" },
            ],
            deviations: ["kept routing unchanged"],
            parser_version: 1,
          },
        }),
        attempt(2, "running", {
          live: {
            activity: "checking branch protection rules",
            observed_at: "2026-08-10T22:59:42Z",
          },
          ended_at: null,
          rationale: {
            raw: null,
            parse_status: "none",
            paths: [],
            deviations: [],
            parser_version: 1,
          },
        }),
      ],
    }),
    node("push_gate", "push gate", "waiting", {
      decision: {
        register: "fact",
        basis: "policy.next_action",
        outcomes: [
          { when: "head moved", then: "review (opus)" },
          { when: "head unchanged", then: "escalate, no attempts left" },
          { when: "turn times out", then: "escalate, no attempts left" },
        ],
      },
    }),
    node("review", "review", "future"),
  ],
});

const escalated = run("escalated", "escalated", {
  dbos_status: "SUCCESS",
  nodes: [
    node("implement", "implement", "escalated"),
    node("push_gate", "push gate", "refused", {
      evidence: {
        kind: "branch_head",
        summary: "head 86dcbf41 observed on the remote after the turn",
      },
    }),
    node("review", "review", "cancelled", {
      note: "never dispatched: nothing was verified for review",
    }),
  ],
});

const queued = run("queued", "queued", {
  dbos_status: "ENQUEUED",
  nodes: [
    node("implement", "implement", "queued", {
      queue: { name: "codex", position: 2 },
    }),
    node("push_gate", "push gate", "future"),
    node("review", "review", "future"),
  ],
});

const cancelled = run("cancelled", "cancelled", {
  nodes: [
    node("implement", "implement", "failed", {
      attempts: [attempt(1, "failed")],
    }),
    node("push_gate", "push gate", "cancelled"),
    node("review", "review", "cancelled"),
  ],
});

const approved = run("approved", "approved", {
  dbos_status: "SUCCESS",
  nodes: [
    node("implement", "implement", "done", { attempts: [attempt(1, "done")] }),
    node("push_gate", "push gate", "passed"),
    node("review", "review", "done", {
      verdict: {
        value: "approve",
        excerpt: "The change is ready to merge.",
        commit_sha: "86dcbf41",
        commit_url: "https://github.com/jomcgi/homelab/commit/86dcbf41",
      },
    }),
  ],
});

const requestChanges = run("request-changes", "changes_requested", {
  dbos_status: "SUCCESS",
  nodes: [
    node("implement", "implement", "done", { attempts: [attempt(1, "done")] }),
    node("push_gate", "push gate", "passed"),
    node("review", "review", "done", {
      verdict: {
        value: "request_changes",
        excerpt: "Please address the requested changes before merging.",
        commit_sha: "86dcbf41",
        commit_url: "https://github.com/jomcgi/homelab/commit/86dcbf41",
      },
    }),
  ],
});

const terminalExample = run("terminal-example", "approved", {
  dbos_status: "SUCCESS",
  completed_at: "2026-08-10T22:55:00Z",
});

const stranded = run("stranded", "stranded", {
  app_version: "old-build",
  server_app_version: "new-build",
});

const unpinned = run("unpinned", "running", {
  plan: {
    ...plan,
    pinned: false,
    source: "current_deploy",
    max_attempts: null,
  },
  nodes: [
    node("implement", "implement", "running", {
      attempts: [attempt(1, "running")],
    }),
    node("push_gate", "push gate", "waiting", {
      decision: {
        register: "belief",
        basis: "policy.next_action with an unrecorded retry bound",
        outcomes: [
          { when: "head moved", then: "review (opus)" },
          {
            when: "head unchanged",
            then: "retry or escalate; the bound was not recorded for this run",
          },
        ],
      },
    }),
    node("review", "review", "future"),
  ],
});

const wide = run("wide", "running", {
  nodes: [
    node("implement", "implement", "done", {
      deps: [],
      attempts: [attempt(1, "done")],
    }),
    node("lint", "lint", "running", {
      deps: ["implement"],
      attempts: [attempt(1, "running")],
    }),
    node("review", "review", "running", {
      deps: ["implement"],
      attempts: [attempt(1, "running", { model: "opus" })],
    }),
    node("push_gate", "push gate", "future", { deps: ["lint", "review"] }),
  ],
});

export const RUN_FIXTURES = {
  running: entry(running, [
    {
      id: 171,
      local_session_id: "fixture-running",
      status: "running",
      model: "luna",
    },
  ]),
  escalated: entry(escalated),
  queued: entry(queued),
  cancelled: entry(cancelled),
  approved: entry(approved),
  "request-changes": entry(requestChanges),
  "terminal-example": entry(terminalExample),
  stranded: entry(stranded),
  unpinned: entry(unpinned, [
    {
      id: 171,
      local_session_id: "fixture-unpinned",
      status: "running",
      model: "luna",
    },
  ]),
  wide: entry(wide),
  "home-with-activity": homeEntry(
    "home-with-activity",
    [homeActivityRun],
    [
      homeSession(201, "Test session 123 acknowledged", "2026-08-07T22:53:00Z"),
      homeSession(202, "add docstring to queues.py", "2026-08-06T22:53:00Z", {
        total_cost_usd: 0,
      }),
    ],
  ),
  "home-with-attention": homeEntry(
    "home-with-attention",
    [homeAttentionRun],
    [homeSession(203, "quiet session", "2026-08-09T22:53:00Z")],
  ),
  "home-empty": homeEntry("home-empty", [], []),
  "home-stale-tier": homeEntry("home-stale-tier", [homeActivityRun], [], {
    engine_tier: "stale",
    snapshot_age_seconds: 180,
  }),
};
