import { describe, expect, test } from "vitest";
import { decidePoll } from "./poll.js";
import { applyLedgerRows, askKey, emptyStage, surfaceKey } from "./stage.js";

function row(overrides = {}) {
  return {
    id: 1,
    companion_id: "companion-1",
    session_id: 512,
    call: "show",
    payload: { surface: "run", ref: "wf-1" },
    principal_subject: "anonymous",
    principal_authority: "anonymous",
    created_at: "2026-08-16T14:02:11Z",
    ...overrides,
  };
}

describe("companion ledger poll", () => {
  test("ledger 404 forgets the companion so the page can re-register", () => {
    expect(decidePoll({ ok: false, status: 404, rows: [] }, 7)).toEqual({
      forget: true,
      rows: [],
      cursor: 0,
    });
  });

  test("ledger 502 keeps the last stage and cursor", () => {
    expect(decidePoll({ ok: false, status: 502, rows: [] }, 7)).toEqual({
      forget: false,
      rows: [],
      cursor: 7,
    });
  });

  test("cursor advances to the max row id", () => {
    const rows = [row({ id: 3 }), row({ id: 11 }), row({ id: 8 })];
    const decision = decidePoll({ ok: true, status: 200, rows }, 2);
    expect(decision.forget).toBe(false);
    expect(decision.cursor).toBe(11);
    expect(decision.rows).toEqual(rows);
  });

  test("a reload from cursor 0 replays all rows and rebuilds the same stage", () => {
    const rows = [
      row({
        id: 1,
        call: "attach",
        session_id: 512,
        payload: { session_id: 512 },
      }),
      row({ id: 2, payload: { surface: "run", ref: "wf-1" } }),
      row({
        id: 3,
        call: "ask",
        payload: { question: "Ship?", options: ["Yes"], ref: "run-1" },
      }),
    ];
    const first = decidePoll(
      { ok: true, status: 200, rows: rows.slice(0, 1) },
      0,
    );
    const rest = decidePoll(
      { ok: true, status: 200, rows: rows.slice(1) },
      first.cursor,
    );
    const incremental = applyLedgerRows(
      applyLedgerRows(emptyStage(), first.rows),
      rest.rows,
    );
    const reload = decidePoll({ ok: true, status: 200, rows }, 0);
    expect(reload.cursor).toBe(3);
    const rebuilt = applyLedgerRows(emptyStage(), reload.rows);
    expect(rebuilt).toEqual(incremental);
    expect(rebuilt.attachedSessionId).toBe(512);
    expect(rebuilt.cards.map((card) => card.key)).toEqual([
      askKey(3),
      surfaceKey("run", "wf-1"),
    ]);
  });
});
