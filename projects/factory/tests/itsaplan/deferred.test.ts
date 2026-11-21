// The integration_test target overlays this onto pinned upstream itsaplan.
import { beforeEach, expect, test } from "bun:test";
import {
  db,
  agentRun,
  issue,
  issueActivity,
  issueLabel,
  projectColumn,
  projectMember,
  teamMember,
  user,
} from "@repo/db";
import { and, eq, sql } from "drizzle-orm";
import {
  app,
  apiKeyApi,
  authedApi,
  createProject,
} from "./__tests__/helpers/app";
import { signUpTestUser } from "./__tests__/helpers/auth";
import { createAgent, setAgentProjectRole } from "./__tests__/helpers/agents";
import { resetDb } from "./__tests__/helpers/db";
import { factoryRoutes } from "./factory";
import { setSubtaskAutomationSettings } from "./modules/projects/service";

app.use(factoryRoutes);
beforeEach(resetDb);

async function fixture(email?: string) {
  const owner = await signUpTestUser({ email });
  const client = authedApi(owner.cookie);
  expect(
    (await createProject(client, { key: "HOME", name: "Homelab" })).status,
  ).toBe(201);
  const project = (await client.projects({ projectKey: "HOME" }).get()).data!;
  const backlog = project.columns.find((c) => c.name === "Backlog")!;
  const todo = project.columns.find((c) => c.name === "Todo")!;
  const label = (
    await client
      .projects({ projectKey: "HOME" })
      .labels.post({ name: "deferred", color: "#123456" })
  ).data!;
  const coder = (
    await createAgent(client, "HOME", {
      name: "Coder",
      username: "coder",
      kind: "external",
      runnerScope: "team",
      triggerOnAssign: true,
    })
  ).data!;
  const coordinator = (
    await createAgent(client, "HOME", {
      name: "Coordinator",
      username: "coordinator",
      kind: "external",
      runnerScope: "team",
    })
  ).data!;
  const created = await client.projects({ projectKey: "HOME" }).issues.post({
    title: "Deferred assessment",
    columnId: backlog.id,
    startDate: "2026-01-01",
    labelIds: [label.id],
  });
  expect(created.status).toBe(201);
  const id = created.data!.id;
  const body = {
    projectId: project.project.id,
    startDate: "2026-01-01",
    delegateUserId: coder.agent.userId,
  };
  const send = (
    headers: Record<string, string> = { "x-api-key": coordinator.apiKey! },
  ) =>
    app.handle(
      new Request(`http://localhost/issues/${id}/factory-deferred-start`, {
        method: "POST",
        headers: { "content-type": "application/json", ...headers },
        body: JSON.stringify(body),
      }),
    );
  return {
    id,
    body,
    send,
    backlog,
    todo,
    coder,
    coordinator,
    owner,
    client,
    project,
  };
}

test("run phase reads exclude stale attempts and require issue access", async () => {
  const { id, coder, owner, send } = await fixture();
  expect((await send()).status).toBe(200);
  await db
    .update(agentRun)
    .set({ nextAttemptAt: new Date(0) })
    .where(eq(agentRun.issueId, id));
  const run = (await apiKeyApi(coder.apiKey!)["agent-runs"].claim.post()).data!
    .run!;
  let reads = 0;
  let current = {
    runId: run.id,
    attempt: run.attempts,
    phase: "waiting",
    reason: "Waiting for an agent slot.",
  };
  const source = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    fetch(request) {
      reads++;
      expect(new URL(request.url).searchParams.get("issueId")).toBe(String(id));
      return Response.json({
        items: [
          current,
          { ...current, attempt: run.attempts - 1 },
          { ...current, runId: run.id + 1 },
        ],
      });
    },
  });
  process.env.FACTORY_RUN_PHASE_URL = source.url.href;
  try {
    const url = `http://localhost/issues/${id}/factory-run-phases`;
    expect((await app.handle(new Request(url))).status).toBe(401);
    expect(reads).toBe(0);
    const response = await app.handle(
      new Request(url, { headers: { cookie: owner.cookie } }),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ items: [current] });
    const read = () =>
      app.handle(new Request(url, { headers: { cookie: owner.cookie } }));
    current = { ...current, phase: "finishing", reason: "" };
    expect(await (await read()).json()).toEqual({ items: [current] });
    await db
      .update(agentRun)
      .set({ nextAttemptAt: new Date(0) })
      .where(eq(agentRun.id, run.id));
    expect(await (await read()).json()).toEqual({ items: [current] });
    await db
      .update(agentRun)
      .set({ nextAttemptAt: new Date(Date.now() + 60_000) })
      .where(eq(agentRun.id, run.id));
    const result = await apiKeyApi(coder.apiKey!)
      ["agent-runs"]({
        runId: run.id,
      })
      .result.post({
        attempt: run.attempts,
        status: "success",
        output: "done",
      });
    expect(result.status).toBe(204);
    expect(await (await read()).json()).toEqual({ items: [current] });
    await db
      .update(agentRun)
      .set({ attempts: run.attempts + 1 })
      .where(eq(agentRun.id, run.id));
    expect(await (await read()).json()).toEqual({ items: [] });

    const oldWrite = await app.handle(
      new Request(`http://localhost/agent-runs/${run.id}/factory-phase`, {
        method: "POST",
        headers: { "x-api-key": coder.apiKey! },
      }),
    );
    expect(oldWrite.status).toBe(404);
  } finally {
    delete process.env.FACTORY_RUN_PHASE_URL;
    source.stop(true);
  }
});

async function effects(id: number) {
  return {
    comments: await db
      .select()
      .from(issueActivity)
      .where(
        and(eq(issueActivity.issueId, id), eq(issueActivity.kind, "comment")),
      ),
    runs: await db.select().from(agentRun).where(eq(agentRun.issueId, id)),
    issue: (await db.select().from(issue).where(eq(issue.id, id)))[0],
    receipts: await db.execute(
      sql`select * from factory_deferred_start where issue_id = ${id}`,
    ),
  };
}

test("real coordinator delegation queues one run and concurrent replays reuse it", async () => {
  const f = await fixture();
  const responses = await Promise.all([f.send(), f.send(), f.send()]);
  expect(responses.map((r) => r.status)).toEqual([200, 200, 200]);
  const receipts = await Promise.all(responses.map((r) => r.json()));
  expect(receipts[1]).toEqual(receipts[0]);
  expect(receipts[2]).toEqual(receipts[0]);
  const state = await effects(f.id);
  expect(state.comments).toHaveLength(1);
  expect(state.comments[0].actorUserId).toBe(f.coordinator.agent.userId);
  expect(state.issue.columnId).toBe(f.todo.id);
  expect(state.issue.delegateUserId).toBe(f.coder.agent.userId);
  expect(state.runs).toHaveLength(1);
  expect(state.runs[0].id).toBe((receipts[0] as { runId: number }).runId);
  expect(state.runs[0].agentId).toBe(f.coder.agent.id);
  expect(state.runs[0].trigger).toBe("delegation");
  expect(state.receipts).toHaveLength(1);
  const activity = await db
    .select()
    .from(issueActivity)
    .where(eq(issueActivity.issueId, f.id))
    .orderBy(issueActivity.id);
  expect(
    activity
      .filter((row) => row.id >= state.comments[0].id)
      .map((row) => (row.kind === "comment" ? "comment" : row.action)),
  ).toEqual(["comment", "status", "delegate"]);
});

for (const failure of ["throw", "skip"]) {
  test(`real enqueue ${failure} rolls back delegate, move and comment; retry starts once`, async () => {
    const f = await fixture();
    const triggerBody =
      failure === "throw"
        ? "raise exception 'injected enqueue failure';"
        : "return null;";
    await db.execute(
      sql.raw(
        "create function factory_test_fail_enqueue() returns trigger language plpgsql as $$ begin " +
          triggerBody +
          " end $$",
      ),
    );
    await db.execute(sql`create trigger factory_test_fail_enqueue before insert on agent_run
    for each row execute function factory_test_fail_enqueue()`);
    try {
      expect((await f.send()).status).toBe(500);
      const state = await effects(f.id);
      expect(state.issue.delegateUserId).toBeNull();
      expect(state.issue.columnId).toBe(f.backlog.id);
      expect(state.comments).toHaveLength(0);
      expect(state.runs).toHaveLength(0);
      expect(state.receipts).toHaveLength(0);
    } finally {
      await db.execute(
        sql`drop trigger factory_test_fail_enqueue on agent_run`,
      );
      await db.execute(sql`drop function factory_test_fail_enqueue()`);
    }
    expect((await f.send()).status).toBe(200);
    expect((await f.send()).status).toBe(200);
    const recovered = await effects(f.id);
    expect(recovered.comments).toHaveLength(1);
    expect(recovered.runs).toHaveLength(1);
  });
}

test("existing authentication and project access checks still apply", async () => {
  const f = await fixture();
  expect((await f.send({})).status).toBe(401);
  const outsider = await signUpTestUser();
  expect((await f.send({ cookie: outsider.cookie })).status).toBe(403);
  expect((await effects(f.id)).comments).toHaveLength(0);
});

for (const change of ["delegate", "future", "label", "column", "archived"]) {
  test(`server rechecks ${change} before any write`, async () => {
    const f = await fixture();
    if (change === "label") {
      await db.delete(issueLabel).where(eq(issueLabel.issueId, f.id));
    } else {
      const patch = {
        delegate: { delegateUserId: f.coder.agent.userId },
        future: { startDate: "2999-01-01" },
        column: { columnId: f.todo.id },
        archived: { archivedAt: new Date() },
      }[change]!;
      await db.update(issue).set(patch).where(eq(issue.id, f.id));
    }
    if (change === "future") f.body.startDate = "2999-01-01";
    expect((await f.send()).status).toBe(409);
    expect((await effects(f.id)).comments).toHaveLength(0);
    expect((await effects(f.id)).runs).toHaveLength(0);
  });
}

test("WIP refusal rolls back; capacity returning starts once with the same key", async () => {
  const f = await fixture();
  // One existing Todo issue fills the column.
  await db
    .update(projectColumn)
    .set({ wipLimit: 1, wipMode: "hard" })
    .where(eq(projectColumn.id, f.todo.id));
  const [occupant] = await db
    .insert(issue)
    .values({
      projectId: f.body.projectId,
      sequenceNumber: 2,
      columnId: f.todo.id,
      title: "Full",
    })
    .returning();
  for (let attempt = 0; attempt < 2; attempt++) {
    const response = await f.send();
    expect(response.status).toBe(409);
    expect((await response.json()).code).toBe("wip_limit_exceeded");
    const state = await effects(f.id);
    expect(state.comments).toHaveLength(0);
    expect(state.runs).toHaveLength(0);
    expect(state.receipts).toHaveLength(0);
    expect(state.issue.columnId).toBe(f.backlog.id);
    expect(state.issue.delegateUserId).toBeNull();
  }
  await db
    .update(issue)
    .set({ columnId: f.backlog.id })
    .where(eq(issue.id, occupant.id));
  expect((await f.send()).status).toBe(200);
  expect((await f.send()).status).toBe(200);
  const recovered = await effects(f.id);
  expect(recovered.comments).toHaveLength(1);
  expect(recovered.runs).toHaveLength(1);
  expect(recovered.receipts).toHaveLength(1);
  expect(recovered.issue.columnId).toBe(f.todo.id);
  expect(recovered.issue.delegateUserId).toBe(f.coder.agent.userId);
});

test("the sync key can restore and update only its own INTSEC finding", async () => {
  const owner = authedApi((await signUpTestUser()).cookie);
  const projectKey = "INTSEC";
  const project = await createProject(owner, {
    key: projectKey,
    name: "Security",
  });
  expect(project.status).toBe(201);
  const setup = owner.projects({ projectKey });
  const { columns } = (await setup.get()).data!;
  const label = (await setup.labels.post({ name: "semgrep" })).data!;
  const agent = (
    await createAgent(owner, projectKey, {
      username: "intsec-sync",
      name: "INTSEC sync",
      kind: "external",
    })
  ).data!;
  const role = await owner.teams({ teamId: project.data!.teamId }).roles.post({
    name: "INTSEC sync",
    permissions: { work_items: { read: true, create: true } },
  });
  expect(role.status).toBe(201);
  expect(
    (
      await setAgentProjectRole(
        owner,
        projectKey,
        agent.agent.userId,
        role.data!.id,
      )
    ).status,
  ).toBe(204);
  const sync = apiKeyApi(agent.apiKey!);
  const finding = "sast/97";
  const description = `Semgrep finding ID: ${finding}\n\nOriginal`;
  const fields = {
    title: "Finding",
    description,
    columnId: columns.find((c) => c.stateType === "backlog")!.id,
    labelIds: [label.id],
  };
  const own = await sync.projects({ projectKey }).issues.post(fields);
  const human = await setup.issues.post(fields);
  expect([own.status, human.status]).toEqual([201, 201]);
  const archivedAt = new Date();
  for (const id of [own.data!.id, human.data!.id])
    await db.update(issue).set({ archivedAt }).where(eq(issue.id, id));

  const payload = {
    finding,
    restore: true,
    title: "Updated",
    description: description + " updated",
    columnId: columns.find((c) => c.stateType === "completed")!.id,
  };
  const send = (id: number) =>
    app.handle(
      new Request(`http://localhost/issues/${id}/factory-semgrep`, {
        method: "PATCH",
        headers: {
          "content-type": "application/json",
          "x-api-key": agent.apiKey!,
        },
        body: JSON.stringify(payload),
      }),
    );
  expect(
    (
      await sync
        .issues({ issueId: human.data!.id })
        .patch({ title: "Direct edit" })
    ).status,
  ).toBe(403);
  const before = await effects(human.data!.id);
  expect((await send(human.data!.id)).status).toBe(403);
  expect(await effects(human.data!.id)).toEqual(before);
  // With subtask automation on, closing a finding linked to a human-owned
  // parent would move that parent. The route refuses any linked finding.
  await setSubtaskAutomationSettings(project.data!.id, {
    completeParent: true,
    closeSubtasks: true,
  });
  const parent = await setup.issues.post({ ...fields, title: "Human parent" });
  expect(parent.status).toBe(201);
  await db
    .update(issue)
    .set({ parentId: parent.data!.id })
    .where(eq(issue.id, own.data!.id));
  const parentBefore = await effects(parent.data!.id);
  expect((await send(own.data!.id)).status).toBe(409);
  expect(await effects(parent.data!.id)).toEqual(parentBefore);
  await db
    .update(issue)
    .set({ parentId: null })
    .where(eq(issue.id, own.data!.id));
  await setSubtaskAutomationSettings(project.data!.id, {
    completeParent: false,
    closeSubtasks: false,
  });

  expect((await send(own.data!.id)).status).toBe(200);
  expect((await effects(own.data!.id)).issue).toMatchObject({
    archivedAt: null,
    title: payload.title,
    description: payload.description,
    columnId: payload.columnId,
  });
});

test("connector records Joe's pick as joesessions and restores the removed delegate", async () => {
  const f = await fixture("joe@example.com");
  const password = "fixture-session-password";
  const sessions = await signUpTestUser({
    email: "joe-sessions@jomcgi.dev",
    password,
    team: false,
  });
  await db
    .update(user)
    .set({ username: "joesessions" })
    .where(eq(user.id, sessions.userId));
  await db
    .insert(teamMember)
    .values({ teamId: f.project.project.teamId, userId: sessions.userId });
  await db
    .insert(projectMember)
    .values({ projectId: f.project.project.id, userId: sessions.userId });
  const item = f.client.issues({ issueId: f.id });
  expect(
    (
      await item.patch({
        columnId: f.todo.id,
        assigneeUserId: f.owner.userId,
        delegateUserId: f.coder.agent.userId,
      })
    ).status,
  ).toBe(200);
  expect(
    (
      await item.patch({
        delegateUserId: null,
        description: "## Choices\n1. Ship (Recommended)\n2. Wait",
      })
    ).status,
  ).toBe(200);

  const server = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    fetch: (request) => app.handle(request),
  });
  try {
    const probe = Bun.spawn([process.env.DECISION_PROBE!], {
      env: {
        ...process.env,
        TEST_PLAN_URL: server.url.origin,
        TEST_READ_KEY: f.coordinator.apiKey!,
        TEST_SESSION_PASSWORD: password,
      },
      stderr: "inherit",
    });
    expect(await probe.exited).toBe(0);
    const state = await effects(f.id);
    expect(state.comments.map((c) => [c.actorUserId, c.body])).toEqual([
      [sessions.userId, "Decision: Wait"],
    ]);
    expect(state.issue.delegateUserId).toBe(f.coder.agent.userId);
  } finally {
    server.stop(true);
  }
}, 30000);

test("the coordinator enters Reviewing once per head and preserves manual moves", async () => {
  const { owner, coder, id } = await fixture();
  const ownerAPI = authedApi(owner.cookie);
  const project = ownerAPI.projects({ projectKey: "HOME" });
  const started = (await project.get()).data!.columns.find(
    (column) => column.name === "In Progress",
  )!;
  const reviewing = await project.columns.post({
    name: "Reviewing",
    stateType: "started",
  });
  expect(reviewing.status).toBe(201);
  const coordinator = (
    await createAgent(ownerAPI, "HOME", {
      name: "Factory coordinator",
      username: "factory-coordinator",
      kind: "external",
    })
  ).data!;
  const move = (key: string, head: string) =>
    app.handle(
      new Request(`http://localhost/issues/${id}/factory-reviewing`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-api-key": key },
        body: JSON.stringify({ head }),
      }),
    );
  const setStarted = async () => {
    expect((await ownerAPI.issues({ issueId: id }).patch({ columnId: started.id })).status).toBe(200);
  };

  await setStarted();
  const head = "a".repeat(40);
  expect((await move(coder.apiKey!, head)).status).toBe(403);
  const hold = await project.labels.post({ name: "needs-human" });
  const labelIds = [hold.data!.id];
  expect((await ownerAPI.issues({ issueId: id }).patch({ labelIds })).status).toBe(200);
  expect(await (await move(coordinator.apiKey!, head)).json()).toEqual({ moved: false });
  expect((await ownerAPI.issues({ issueId: id }).patch({ labelIds: [] })).status).toBe(200);
  const applied = await move(coordinator.apiKey!, head);
  expect(applied.status).toBe(200);
  expect(await applied.json()).toEqual({ moved: true });
  expect((await ownerAPI.issues({ issueId: id }).get()).data?.columnId).toBe(reviewing.data!.id);

  await setStarted();
  expect(await (await move(coordinator.apiKey!, head)).json()).toEqual({ moved: false });
  expect((await ownerAPI.issues({ issueId: id }).get()).data?.columnId).toBe(started.id);
});
