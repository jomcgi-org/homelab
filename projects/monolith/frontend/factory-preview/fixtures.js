// Entirely invented data. Never copy a production response into this module.
export const NOW = "2026-10-03T12:00:00.000Z";
export const LONG_TITLE =
  "Synthetic task: verify a deliberately long factory title stays readable while reviewing the queued work and its complete implementation history";
export const LONG_TOKEN =
  "synthetic-supercalifragilisticexpialidocious-".repeat(4);
const ago = (minutes) =>
  new Date(Date.parse(NOW) - minutes * 60_000).toISOString();
const days = Array.from({ length: 14 }, (_, i) =>
  ago((13 - i) * 1440).slice(0, 10),
);

function task(index, state = "landed") {
  return {
    issue_number: 900000 + index,
    title:
      index === 1
        ? LONG_TITLE
        : `Synthetic ${index}: ${index === 2 ? LONG_TOKEN : "inspect the fixture build and explain its test results"}`,
    state,
    phase: state === "in flight" ? "review" : "complete",
    task_class: "delivery",
    admitted_at: ago(index === 1 ? 47 : 60 + index * 10),
    deadline_at: ago(-73),
    finished_at:
      state === "landed" || state === "failed" ? ago(index * 10) : null,
    turns_used: 4,
    allowance_turns: 12,
    cost_usd: 1.23 + index / 100,
    review_rounds: 1,
    pr: null,
    nodes: [
      { node_key: "plan", state: "done", attempts: [] },
      {
        node_key: "review",
        state: state === "in flight" ? "running" : "done",
        attempts: [],
      },
    ],
  };
}

export function payloads(scenario = "live") {
  const populated = scenario === "live";
  const entity = {
    kind: "project",
    slug: "synthetic-project",
    title: "Synthetic project: a deliberately long context record title",
    scope: "invented fixture",
    note_counts: { verified: 24, unverified: 2 },
  };
  const notes = populated
    ? Array.from({ length: 26 }, (_, i) => ({
        note_id: `synthetic-note-${i + 1}`,
        title:
          i === 0
            ? LONG_TITLE
            : `Synthetic record ${i + 1}: ${i === 1 ? LONG_TOKEN : "the invented worker uses a local queue"}`,
        snippet:
          "This is invented fixture content. It does not describe a real project, person, session, or operational event.",
        verification_state: i < 24 ? "verified" : "unverified",
        observed_at: ago(i * 60),
        confidence: 0.9,
        disputed: false,
        scope: "invented fixture",
        entities: [entity],
      }))
    : [];
  const board = {
    state: "enabled",
    snapshotted_at: NOW,
    policy: {
      generation: 1,
      max_tasks: 4,
      max_task_turns_hard: 20,
      max_review_rounds: 3,
      task_budget_usd: 30,
    },
    active: populated ? [task(1, "in flight"), task(2, "landing")] : [],
    queued: populated ? [task(3, "queued")] : [],
    recent: populated
      ? Array.from({ length: 25 }, (_, i) =>
          task(i + 10, i === 3 ? "failed" : "landed"),
        )
      : [],
    totals_7d: {
      landed: populated ? 24 : 0,
      escalated: populated ? 1 : 0,
      spend_usd: populated ? 37.5 : 0,
    },
  };
  const facts = {
    daily: populated
      ? days.map((d, i) => ({ d, verified: 5 + i, unverified: 3 + (i % 3) }))
      : [],
    totals: {
      verified: populated ? 24 : 0,
      unverified: populated ? 2 : 0,
      disputed: 0,
    },
    contradictions: populated ? 1 : 0,
  };
  return {
    "/slop/factory/data/activity": {
      now: { active_last_hour: board.active.length },
      daily: populated
        ? days.flatMap((day, i) =>
            ["luna", "sol", "claude-sonnet", "spark", "other"].map(
              (model, j) => ({
                day,
                model,
                sessions: ((i + j) % 8) + 1,
                input_tokens: 12000 + i * 300,
                output_tokens: 3000,
              }),
            ),
          )
        : [],
      local_daily: [],
      spend_daily: populated
        ? days.map((day, i) => ({ day, spend_usd: 2 + i / 2 }))
        : [],
      totals_7d: {
        ember: {},
        local: {},
        combined: {
          sessions: populated ? 164 : 0,
          input_tokens: populated ? 1500000 : 0,
          output_tokens: populated ? 42000 : 0,
          spend_usd: populated ? 37.5 : 0,
        },
      },
    },
    "/slop/factory/merges": {
      daily: populated
        ? days.map((d, i) => ({
            d,
            feat: (i % 3) + 1,
            fix: (i % 4) + 1,
            docs: i % 2,
            chore: 1,
            test: 0,
            refactor: 0,
            other: 0,
          }))
        : [],
      week: populated
        ? Array.from({ length: 24 }, (_, i) => ({
            number: 910000 + i,
            title: `fix(synthetic): ${i === 0 ? LONG_TITLE : i === 1 ? LONG_TOKEN : `Invented merged change ${i + 1} with a readable explanation`}`,
            scope: i % 2 ? "synthetic-worker" : "synthetic-ui",
            type: i % 2 ? "fix" : "feat",
            additions: 120 + i,
            deletions: 7,
            merged_at: ago(i * 20),
          }))
        : [],
      totals: { n_7d: populated ? 24 : 0 },
      snapshotted_at: NOW,
    },
    "/slop/factory/facts": facts,
    "/slop/factory/goals": {
      goals: populated
        ? [
            {
              statement:
                "Synthetic goal: retain all status information when long records wrap on a phone",
              declared_by: "invented fixture",
              issue_numbers: [900001, 900002],
              issues_closed: 1,
              linked_issues: 2,
              issues_unknown: 0,
              last_activity: NOW,
              merged_refs: 3,
              stale: false,
            },
          ]
        : [],
      declared_at: NOW,
      stale: false,
    },
    "/slop/factory/data/board": board,
    "/slop/factory/entities": populated ? [entity] : [],
    "/slop/factory/entities/project/synthetic-project/notes?state=verified%2Cunverified&limit=60":
      {
        entity,
        notes,
        contradictions: populated ? [{ a: notes[0], b: notes[24] }] : [],
      },
    "/slop/factory/search?q=Synthetic&limit=30": notes,
    "/slop/factory/search-index": {
      states: ["verified", "unverified"],
      entities: ["synthetic-project"],
      notes: notes.map((note, i) => [
        note.note_id,
        note.title,
        i < 24 ? 0 : 1,
        0,
      ]),
    },
  };
}
