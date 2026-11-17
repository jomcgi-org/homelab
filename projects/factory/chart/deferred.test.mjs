import assert from "node:assert/strict";
import { test } from "node:test";
import { startDeferred } from "./files/itsaplan/deferred.ts";
import { transactionScope } from "./files/itsaplan/transaction.ts";

const request = {
  issueId: 118,
  projectId: 7,
  startDate: "2026-11-14",
  actorUserId: "coordinator",
  delegateUserId: "coder",
  key: "deferred/118/2026-11-14",
};

// Model database commits, not HTTP acceptance. The services import one shared
// db, just as upstream does. Every injected failure must roll back ALL effects.
function fixture() {
  let state = { writes: [], runs: [], delegate: null, receipt: null };
  let fail = "";
  let tail = Promise.resolve();
  const check = (point) => {
    if (fail === point) throw new Error(`failure at ${point}`);
  };
  const pool = {
    get state() {
      return state;
    },
    transaction(fn) {
      // The production adapter takes SELECT FOR UPDATE on this issue before
      // reading the receipt. Concurrent requests serialize at that row lock.
      const result = tail.then(async () => {
        const tx = { state: structuredClone(state) };
        const result = await fn(tx);
        check("commit");
        state = tx.state;
        return result;
      });
      tail = result.catch(() => {});
      return result;
    },
  };
  const { db, transaction } = transactionScope(pool);
  const api = {
    transaction,
    conflict: (message) => new Error(message),
    lockIssue: async () => check("before-send"),
    receipt: async () => db.state.receipt,
    hasRun: async (id) => db.state.runs.includes(id),
    eligible: async () => {
      if (db.state.delegate) throw new Error("already delegated");
      return { todoId: 2, agentId: 3 };
    },
    comment: async (comment) => {
      db.state.writes.push(["comment", comment.body]);
      check("comment");
    },
    update: async (_id, patch) => {
      if (patch.columnId) {
        db.state.writes.push(["move", patch.columnId]);
        check("move");
      } else {
        db.state.delegate = patch.delegateUserId;
        db.state.writes.push(["delegate", patch.delegateUserId]);
        // v1.10.0 stores the delegate, then calls enqueueDelegateRun. Its
        // failure or silent skip must not leave the delegate committed.
        check("delegate-before-enqueue");
        if (fail === "skip-enqueue") return;
        db.state.runs.push(42);
        check("run");
      }
    },
    runs: async () => [...db.state.runs],
    saveReceipt: async (r, runId) => {
      db.state.receipt = { ...r, runId };
      check("receipt");
    },
  };
  return {
    start: (r = request) => startDeferred(api, r),
    fail: (point) => (fail = point),
    state: () => state,
  };
}

function assertStarted(f) {
  assert.deepEqual(f.state().writes, [
    [
      "comment",
      "Starting deferred work scheduled for 2026-11-14 (UTC).\n\nFactory start: deferred/118/2026-11-14.",
    ],
    ["move", 2],
    ["delegate", "coder"],
  ]);
  assert.deepEqual(f.state().runs, [42]);
  assert.equal(f.state().receipt.runId, 42);
}

test("start, lost response and concurrent retries have one ordered set of effects", async () => {
  const f = fixture();
  const results = await Promise.all([f.start(), f.start(), f.start()]);
  assert.deepEqual(results, Array(3).fill({ key: request.key, runId: 42 }));
  assertStarted(f);
  await f.start();
  assertStarted(f);
});

for (const point of [
  "before-send",
  "comment",
  "move",
  "delegate-before-enqueue",
  "skip-enqueue",
  "run",
  "receipt",
  "commit",
]) {
  test(`${point}: rollback leaves the issue eligible for a retry`, async () => {
    const f = fixture();
    f.fail(point);
    await assert.rejects(f.start());
    assert.deepEqual(f.state(), {
      writes: [],
      runs: [],
      delegate: null,
      receipt: null,
    });
    f.fail("");
    await f.start();
    assertStarted(f);
  });
}

test("a previous delegation run cannot substitute for this start's run", async () => {
  const f = fixture();
  f.state().runs.push(7);
  f.fail("skip-enqueue");
  await assert.rejects(f.start(), /exactly one run/);
  assert.deepEqual(f.state().runs, [7]);
  assert.equal(f.state().delegate, null);
  assert.equal(f.state().receipt, null);
});

test("a preexisting delegate is never changed or treated as a completed start", async () => {
  const f = fixture();
  f.state().delegate = "coder";
  await assert.rejects(f.start(), /already delegated/);
  assert.deepEqual(f.state().writes, []);
  assert.deepEqual(f.state().runs, []);
});

test("receipt retries verify the run and bind the actor and delegate", async () => {
  const f = fixture();
  await f.start();
  for (const change of [
    { actorUserId: "person" },
    { delegateUserId: "other" },
  ]) {
    await assert.rejects(f.start({ ...request, ...change }), /another request/);
  }
  f.state().runs = [];
  await assert.rejects(f.start(), /run was removed/);
  assert.equal(f.state().writes.length, 3);
});

test("transaction scope isolates concurrent requests from each other and the pool", async () => {
  let next = 0;
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  const pool = {
    name: "pool",
    read() {
      return this.name;
    },
    transaction: (fn) => fn({ name: ++next, read: pool.read }),
  };
  const { db, transaction } = transactionScope(pool);
  const results = [1, 2].map(() =>
    transaction(async () => {
      const before = db.read();
      await gate;
      assert.equal(db.read(), before);
      return before;
    }),
  );
  assert.equal(db.read(), "pool");
  release();
  assert.deepEqual(await Promise.all(results), [1, 2]);
  assert.equal(db.read(), "pool");
  await assert.rejects(
    transaction(async () => {
      throw new Error("rollback");
    }),
  );
  assert.equal(db.read(), "pool");
});
