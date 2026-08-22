import { describe, expect, test } from "vitest";
import {
  applyLedgerRows,
  answerCard,
  askKey,
  cardPhase,
  dismissCard,
  emptyStage,
  renderSummoningCall,
  renderWireCall,
  surfaceKey,
  togglePinned,
} from "./stage.js";

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

describe("companion stage reducer", () => {
  test("show adds a surface card keyed by surface and ref", () => {
    const stage = applyLedgerRows(emptyStage(), [
      row({ id: 1, payload: { surface: "run", ref: "wf-1", focus: "review" } }),
    ]);
    expect(stage.cards).toHaveLength(1);
    expect(stage.cards[0]).toMatchObject({
      key: surfaceKey("run", "wf-1"),
      kind: "surface",
      surface: "run",
      ref: "wf-1",
      focus: "review",
      question: null,
      options: null,
      rowId: 1,
      call: "show",
      pinned: false,
      answered: false,
    });
  });

  test("repeat show moves the refreshed card to the front without duplication", () => {
    const first = applyLedgerRows(emptyStage(), [
      row({
        id: 1,
        payload: { surface: "run", ref: "wf-1", focus: "implement" },
      }),
      row({
        id: 2,
        call: "show",
        payload: { surface: "walkthrough", ref: "turn:7" },
      }),
    ]);
    const stage = applyLedgerRows(first, [
      row({ id: 3, payload: { surface: "run", ref: "wf-1", focus: "review" } }),
    ]);
    expect(stage.cards.map((card) => card.key)).toEqual([
      surfaceKey("run", "wf-1"),
      surfaceKey("walkthrough", "turn:7"),
    ]);
    expect(stage.cards[0]).toMatchObject({
      rowId: 3,
      focus: "review",
      call: "show",
    });
    expect(stage.cards).toHaveLength(2);
  });

  test("dismiss with a surface removes only cards for that surface", () => {
    const shown = applyLedgerRows(emptyStage(), [
      row({ id: 1, payload: { surface: "run", ref: "wf-1" } }),
      row({
        id: 2,
        call: "show",
        payload: { surface: "walkthrough", ref: "turn:7" },
      }),
      row({
        id: 3,
        call: "ask",
        payload: {
          question: "Ship it?",
          options: ["Yes", "No"],
          ref: "review:1",
        },
      }),
    ]);
    const stage = applyLedgerRows(shown, [
      row({ id: 4, call: "dismiss", payload: { surface: "run" } }),
    ]);
    expect(stage.cards.map((card) => card.key)).toEqual([
      askKey(3),
      surfaceKey("walkthrough", "turn:7"),
    ]);
  });

  test("dismiss without a surface clears every model-owned card", () => {
    const shown = applyLedgerRows(emptyStage(), [
      row({ id: 1, payload: { surface: "run", ref: "wf-1" } }),
      row({
        id: 2,
        call: "ask",
        payload: { question: "Ship it?", options: ["Yes"], ref: "review:1" },
      }),
    ]);
    const stage = applyLedgerRows(shown, [
      row({ id: 3, call: "dismiss", payload: {} }),
    ]);
    expect(stage.cards).toEqual([]);
  });

  test("a user-dismissed surface stays gone until a newer row re-shows the same surface:ref", () => {
    const shown = applyLedgerRows(emptyStage(), [
      row({ id: 1, payload: { surface: "run", ref: "wf-1" } }),
    ]);
    const dismissed = dismissCard(shown, surfaceKey("run", "wf-1"));
    const replayed = applyLedgerRows(dismissed, [
      row({ id: 1, payload: { surface: "run", ref: "wf-1" } }),
    ]);
    expect(replayed.cards).toEqual([]);
    const older = applyLedgerRows(replayed, [
      row({ id: 0, payload: { surface: "run", ref: "wf-1" } }),
    ]);
    expect(older.cards).toEqual([]);
  });

  test("a user-dismissed card stays gone until a later show of the same key", () => {
    const shown = applyLedgerRows(emptyStage(), [
      row({ id: 1, payload: { surface: "run", ref: "wf-1" } }),
    ]);
    const dismissed = dismissCard(shown, surfaceKey("run", "wf-1"));
    expect(dismissed.cards).toEqual([]);
    const other = applyLedgerRows(dismissed, [
      row({
        id: 2,
        call: "show",
        payload: { surface: "transcript", ref: "512" },
      }),
    ]);
    expect(other.cards.map((card) => card.key)).toEqual([
      surfaceKey("transcript", "512"),
    ]);
    const reshown = applyLedgerRows(other, [
      row({ id: 3, payload: { surface: "run", ref: "wf-1", focus: "review" } }),
    ]);
    expect(reshown.cards.map((card) => card.key)).toEqual([
      surfaceKey("run", "wf-1"),
      surfaceKey("transcript", "512"),
    ]);
    expect(reshown.cards[0]).toMatchObject({
      rowId: 3,
      focus: "review",
      call: "show",
    });
  });

  test("unknown surface does not throw and adds no card", () => {
    const start = emptyStage();
    const stage = applyLedgerRows(start, [
      row({ id: 1, payload: { surface: "map", ref: "room-1" } }),
      row({ id: 2, payload: { surface: "run", ref: "wf-1" } }),
    ]);
    expect(stage.cards).toHaveLength(1);
    expect(stage.cards[0].key).toBe(surfaceKey("run", "wf-1"));
    expect(stage.cards[0].surface).toBe("run");
  });

  test("rows apply in id order even when the batch arrives shuffled", () => {
    const stage = applyLedgerRows(emptyStage(), [
      row({
        id: 2,
        call: "show",
        payload: { surface: "walkthrough", ref: "turn:7" },
      }),
      row({ id: 1, payload: { surface: "run", ref: "wf-1" } }),
    ]);
    expect(stage.cards.map((card) => card.key)).toEqual([
      surfaceKey("walkthrough", "turn:7"),
      surfaceKey("run", "wf-1"),
    ]);
  });

  test("an empty batch is a no-op on the resulting state", () => {
    const start = applyLedgerRows(emptyStage(), [
      row({
        id: 4,
        call: "attach",
        payload: { session_id: 512 },
      }),
      row({ id: 5, payload: { surface: "run", ref: "wf-1" } }),
    ]);
    const after = applyLedgerRows(start, []);
    expect(after.attachedSessionId).toBe(512);
    expect(after.cards).toEqual(start.cards);
    expect(after.cards).toHaveLength(1);
    expect(after.cards[0].key).toBe(surfaceKey("run", "wf-1"));
    const fromEmpty = applyLedgerRows(emptyStage(), []);
    expect(fromEmpty.cards).toEqual([]);
    expect(fromEmpty.attachedSessionId).toBeNull();
  });

  test("multiple asks may coexist", () => {
    const stage = applyLedgerRows(emptyStage(), [
      row({
        id: 1,
        call: "ask",
        payload: {
          question: "Merge?",
          options: ["Yes", "No"],
          ref: "review:1",
        },
      }),
      row({
        id: 2,
        call: "ask",
        payload: { question: "Retry?", options: ["Yes"], ref: "review:2" },
      }),
    ]);
    expect(stage.cards.map((card) => card.key)).toEqual([askKey(2), askKey(1)]);
  });

  test("two gates about the same ref coexist rather than clobbering", () => {
    const stage = applyLedgerRows(emptyStage(), [
      row({
        id: 1,
        call: "ask",
        payload: {
          question: "Merge?",
          options: ["Yes", "No"],
          ref: "review:1",
        },
      }),
      row({
        id: 2,
        call: "ask",
        payload: {
          question: "The suite is red, still merge?",
          options: ["Yes", "No"],
          ref: "review:1",
        },
      }),
    ]);
    expect(stage.cards.map((card) => card.key)).toEqual([askKey(2), askKey(1)]);
    expect(stage.cards.map((card) => card.question)).toEqual([
      "The suite is red, still merge?",
      "Merge?",
    ]);
  });

  test("dismissing one gate does not suppress a later gate on the same ref", () => {
    const raised = applyLedgerRows(emptyStage(), [
      row({
        id: 1,
        call: "ask",
        payload: { question: "Merge?", options: ["Yes"], ref: "review:1" },
      }),
    ]);
    const dismissed = dismissCard(raised, askKey(1));
    expect(dismissed.cards).toEqual([]);
    const reraised = applyLedgerRows(dismissed, [
      row({
        id: 2,
        call: "ask",
        payload: { question: "Merge now?", options: ["Yes"], ref: "review:1" },
      }),
    ]);
    expect(reraised.cards.map((card) => card.key)).toEqual([askKey(2)]);
    expect(reraised.cards[0].question).toBe("Merge now?");
  });

  test("card phases decay after later exchanges", () => {
    const card = { rowId: 1, kind: "surface", pinned: false };
    expect(cardPhase(card, [row({ id: 1 })])).toBe("front");
    expect(cardPhase(card, [row({ id: 2, call: "ask" })])).toBe("receded");
    expect(
      cardPhase(card, [
        row({ id: 2, call: "show" }),
        row({ id: 3, call: "ask" }),
      ]),
    ).toBe("gone");
  });

  test("pinned cards are exempt from decay and model dismiss", () => {
    const shown = applyLedgerRows(emptyStage(), [
      row({ id: 1, payload: { surface: "run", ref: "wf-1" } }),
    ]);
    const pinned = togglePinned(shown, surfaceKey("run", "wf-1"));
    expect(
      cardPhase(pinned.cards[0], [
        row({ id: 2, call: "show" }),
        row({ id: 3, call: "ask" }),
      ]),
    ).toBe("front");
    const dismissed = applyLedgerRows(pinned, [
      row({ id: 4, call: "dismiss", payload: {} }),
    ]);
    expect(dismissed.cards).toHaveLength(1);
  });

  test("tool cards skip the receded phase", () => {
    const tool = { rowId: 1, kind: "tool", pinned: false };
    expect(cardPhase(tool, [row({ id: 2, call: "show" })])).toBe("front");
    expect(
      cardPhase(tool, [
        row({ id: 2, call: "show" }),
        row({ id: 3, call: "ask" }),
      ]),
    ).toBe("gone");
  });

  test("answering an ask marks only that card answered", () => {
    const stage = applyLedgerRows(emptyStage(), [
      row({
        id: 1,
        call: "ask",
        payload: { question: "Ship?", options: ["Yes"], ref: "run-1" },
      }),
      row({
        id: 2,
        call: "ask",
        payload: { question: "Retry?", options: ["No"], ref: "run-2" },
      }),
    ]);
    const answered = answerCard(stage, askKey(1));
    expect(answered.cards.map((card) => card.answered)).toEqual([false, true]);
  });

  test("renders exact card badges and wire calls", () => {
    const attach = row({
      id: 1,
      call: "attach",
      session_id: 213,
      payload: {},
    });
    const show = row({
      id: 2,
      payload: { surface: "run", ref: "run-84", focus: "push_gate" },
    });
    const ask = row({
      id: 3,
      call: "ask",
      payload: { ref: "run-84", options: ["approve", "send back"] },
    });
    expect(renderSummoningCall(show)).toBe(
      "show(run, run-84, focus=push_gate)",
    );
    expect(renderSummoningCall(ask)).toBe("ask(run-84)");
    expect(renderWireCall(ask)).toBe("ask(run-84, [approve, send back])");
    expect(renderWireCall(attach)).toBe("attach(213)");
  });
});
