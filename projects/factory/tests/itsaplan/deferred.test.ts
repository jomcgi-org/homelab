// The integration_test target overlays this onto pinned upstream itsaplan.
import { beforeEach, expect, test } from "bun:test";
import {
  db,
  agentRun,
  issue,
  issueActivity,
  issueLabel,
  projectColumn,
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

async function fixture() {
  const owner = await signUpTestUser();
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
  return { id, body, send, backlog, todo, coder, coordinator };
}

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

test("concurrent review follow-up requests commit one backlog issue", async () => {
  const f = await fixture();
  const input = {
    key: "review-followup/example/homelab/42/F1",
    columnId: f.backlog.id,
    title: "Follow-up",
    description: "Review details",
  };
  const send = () =>
    app.handle(
      new Request("http://localhost/projects/HOME/factory-review-followups", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": f.coordinator.apiKey!,
        },
        body: JSON.stringify(input),
      }),
    );
  const responses = await Promise.all([send(), send()]);
  expect(responses.map((r) => r.status)).toEqual([200, 200]);
  expect(await responses[0].json()).toEqual(await responses[1].json());
  const matches = await db
    .select()
    .from(issue)
    .where(eq(issue.title, input.title));
  expect(matches).toHaveLength(1);
  expect(matches[0].columnId).toBe(f.backlog.id);
  expect(matches[0].delegateUserId).toBeNull();
});
