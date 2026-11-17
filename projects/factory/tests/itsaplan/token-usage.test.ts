// Run against the pinned itsaplan API and its real migrated PostgreSQL schema.
import { SQL } from "bun";
import { afterAll, beforeEach, expect, test } from "bun:test";
import { db, agentRun, issue, projectColumn } from "@repo/db";
import { eq } from "drizzle-orm";
import { apiKeyApi, authedApi, createProject } from "./__tests__/helpers/app";
import { signUpTestUser } from "./__tests__/helpers/auth";
import { createAgent } from "./__tests__/helpers/agents";
import { resetDb } from "./__tests__/helpers/db";

const connector = new SQL(process.env.FACTORY_CONNECTOR_DATABASE_URL!);
beforeEach(async () => {
  await resetDb();
  await connector`TRUNCATE run, work_item, repo_binding CASCADE`;
});
afterAll(() => connector.close());

async function localRun(
  runId: number,
  issueId: number,
  usage?: { inputTokens: number; outputTokens: number },
) {
  // Revision rounds resolve the issue through work_item; shared runs can be
  // accounted for before they are attached to a work item.
  let workItemId = null;
  if (runId < 0) {
    await connector`INSERT INTO repo_binding
      (name, owner, repo, base_branch, itsaplan_project, kagent_namespace,
       kagent_agent, branch_prefix, max_engineering_retries)
      VALUES ('homelab', 'org', 'homelab', 'main', 'HOME', 'factory', 'coder', 'factory/', 2)
      ON CONFLICT DO NOTHING`;
    const [item] = await connector`INSERT INTO work_item
      (binding_name, issue_id, issue_key, branch, agent_key, kagent_namespace, kagent_agent)
      VALUES ('homelab', ${issueId}, 'HOME-fixture', 'factory/fixture', 'coder', 'factory', 'coder')
      ON CONFLICT (issue_id) DO UPDATE SET issue_id = excluded.issue_id RETURNING id`;
    workItemId = item.id;
  }
  await connector`INSERT INTO run
    (id, issue_id, work_item_id, origin, agent_key, trigger, prompt, system_prompt,
     attempt, state, input_tokens, output_tokens)
    VALUES (${runId}, ${runId > 0 ? issueId : null}, ${workItemId},
      ${runId > 0 ? "itsaplan" : "connector"}, 'coder', 'issue_delegated', '', '', 1,
      'closed', ${usage?.inputTokens ?? null}, ${usage?.outputTokens ?? null})`;
}

async function fixture() {
  const owner = await signUpTestUser();
  const client = authedApi(owner.cookie);
  expect(
    (await createProject(client, { key: "HOME", name: "Homelab" })).status,
  ).toBe(201);
  const project = (await client.projects({ projectKey: "HOME" }).get()).data!;
  const todo = project.columns.find((c) => c.name === "Todo")!;
  const done = project.columns.find((c) => c.stateType === "completed")!;
  const canceled = project.columns.find((c) => c.stateType === "canceled")!;
  const coder = (
    await createAgent(client, "HOME", {
      name: "Coder",
      username: "coder",
      kind: "external",
      triggerOnMention: true,
    })
  ).data!;
  const runner = apiKeyApi(coder.apiKey!);
  async function newIssue(title: string, columnId = todo.id) {
    const response = await client.projects({ projectKey: "HOME" }).issues.post({
      title,
      columnId,
    });
    expect(response.status).toBe(201);
    return response.data!;
  }
  async function run(
    issueId: number,
    usage?: { inputTokens: number; outputTokens: number },
    status: "success" | "failed" = "success",
  ) {
    expect(
      (
        await client.issues({ issueId }).comments.post({
          body: "Please work on this @coder",
        })
      ).status,
    ).toBe(201);
    const claimed = await runner["agent-runs"].claim.post();
    expect(claimed.status).toBe(200);
    const run = claimed.data!.run!;
    expect(run.issueId).toBe(issueId);
    const result = await runner["agent-runs"]({ runId: run.id }).result.post({
      attempt: run.attempts,
      status,
      usage,
    });
    expect(result.status).toBe(204);
    const [stored] = await db
      .select()
      .from(agentRun)
      .where(eq(agentRun.id, run.id));
    expect(stored).toMatchObject({
      issueId,
      status,
      inputTokens: usage?.inputTokens ?? null,
      outputTokens: usage?.outputTokens ?? null,
    });
    return run.id;
  }
  async function move(issueId: number, columnId: number) {
    expect((await client.issues({ issueId }).patch({ columnId })).status).toBe(
      200,
    );
  }
  return { client, project, coder, todo, done, canceled, newIssue, run, move };
}

async function runReport(env: Record<string, string> = {}) {
  // Execute the shipped export/import/report command against both real schemas.
  const child = Bun.spawn(["bash", process.env.USAGE_REPORT!], {
    env: { ...process.env, ...env },
    stdout: "pipe",
    stderr: "pipe",
  });
  const [stdout, stderr, code] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ]);
  return { stdout, stderr, code };
}

async function report() {
  const result = await runReport();
  expect(result.stderr).toBe("");
  expect(result.code).toBe(0);
  const [header, ...rows] = result.stdout.trim().split("\n");
  const keys = header!.split(",");
  // The report contains only issue keys and numbers; empty CSV fields are NULL.
  return rows.map((row) =>
    Object.fromEntries(
      row
        .split(",")
        .map((value, index) => [
          keys[index],
          index === 0 ? value : value === "" ? null : Number(value),
        ]),
    ),
  );
}

test("HTTP run results persist nonzero usage; manual Done sums successful, failed and unknown runs", async () => {
  const f = await fixture();
  const delivered = await f.newIssue("Manual Done");
  const other = await f.newIssue("Still active");
  await f.run(delivered.id, { inputTokens: 100, outputTokens: 10 });
  await f.run(other.id, { inputTokens: 9000, outputTokens: 900 });
  await f.run(delivered.id, { inputTokens: 200, outputTokens: 20 }, "failed");
  await f.run(delivered.id);
  // A canceled run can retain measured usage; the report must count it too.
  await db.insert(agentRun).values({
    agentId: f.coder.agent.id,
    projectId: f.project.project.id,
    issueId: delivered.id,
    prompt: "Canceled after work",
    status: "canceled",
    inputTokens: 400,
    outputTokens: 40,
  });
  expect(await report()).toEqual([]);
  await f.move(delivered.id, f.done.id);
  expect(await report()).toEqual([
    {
      issue_key: "HOME-1",
      runs: 4,
      unmeasured_runs: 1,
      input_tokens: 700,
      output_tokens: 70,
      total_tokens: 770,
    },
  ]);
});

test("consolidated delivery includes shared 300 and revision 200 exactly once, plus unknown rounds", async () => {
  const f = await fixture();
  const delivered = await f.newIssue("Revised before manual Done");
  const other = await f.newIssue("Another issue");
  const usage = { inputTokens: 270, outputTokens: 30 };
  const shared = await f.run(delivered.id, usage);
  await localRun(shared, delivered.id, usage);
  await localRun(-1, delivered.id, { inputTokens: 180, outputTokens: 20 });
  await localRun(-2, delivered.id);
  await localRun(-3, other.id, { inputTokens: 9000, outputTokens: 900 });
  expect(await report()).toEqual([]);
  await f.move(delivered.id, f.done.id);
  expect(await report()).toEqual([
    {
      issue_key: "HOME-1",
      runs: 3,
      unmeasured_runs: 1,
      input_tokens: 450,
      output_tokens: 50,
      total_tokens: 500,
    },
  ]);
});

test("shared runs use local cancellation totals and fall back to itsaplan measurements", async () => {
  const f = await fixture();
  const delivered = await f.newIssue("Canceled work then delivered");
  const [canceled] = await db
    .insert(agentRun)
    .values({
      agentId: f.coder.agent.id,
      projectId: f.project.project.id,
      issueId: delivered.id,
      prompt: "Canceled after work",
      status: "canceled",
    })
    .returning();
  await localRun(canceled!.id, delivered.id, {
    inputTokens: 100,
    outputTokens: 10,
  });
  const shared = await f.run(delivered.id, {
    inputTokens: 200,
    outputTokens: 20,
  });
  await localRun(shared, delivered.id);
  await f.move(delivered.id, f.done.id);
  expect(await report()).toEqual([
    {
      issue_key: "HOME-1",
      runs: 2,
      unmeasured_runs: 0,
      input_tokens: 300,
      output_tokens: 30,
      total_tokens: 330,
    },
  ]);
});

test("an unavailable database fails the command without publishing partial totals", async () => {
  for (const variable of [
    "FACTORY_CONNECTOR_DATABASE_URL",
    "ITSAPLAN_DATABASE_URL",
  ]) {
    const unavailable = new URL(process.env[variable]!);
    unavailable.pathname = "/missing_report_database";
    const result = await runReport({ [variable]: unavailable.toString() });
    expect(result.code).not.toBe(0);
    expect(result.stdout).toBe("");
    expect(result.stderr).toContain("missing_report_database");
  }
});

test("Done history survives reopening, repeated delivery, column changes and archiving", async () => {
  const f = await fixture();
  const reopened = await f.newIssue("Reopened");
  await f.run(reopened.id, { inputTokens: 300, outputTokens: 30 });
  for (let round = 0; round < 2; round++) {
    await f.move(reopened.id, f.done.id);
    await f.move(reopened.id, f.todo.id);
  }
  const archived = await f.newIssue("Archived");
  await f.run(archived.id);
  await f.move(archived.id, f.done.id);
  await db
    .update(issue)
    .set({ archivedAt: new Date() })
    .where(eq(issue.id, archived.id));
  // Use the historical stateType snapshot, not today's meaning of that column.
  await db
    .update(projectColumn)
    .set({ name: "Former Done", stateType: "started" })
    .where(eq(projectColumn.id, f.done.id));
  await f.newIssue("Never delivered", f.canceled.id);
  expect(await report()).toEqual([
    {
      issue_key: "HOME-1",
      runs: 1,
      unmeasured_runs: 0,
      input_tokens: 300,
      output_tokens: 30,
      total_tokens: 330,
    },
    {
      issue_key: "HOME-2",
      runs: 1,
      unmeasured_runs: 1,
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
    },
  ]);
});

test("issues created in Done are included without runs; other projects are excluded", async () => {
  const f = await fixture();
  await f.newIssue("Already Done", f.done.id);
  expect(
    (await createProject(f.client, { key: "OTHER", name: "Another project" }))
      .status,
  ).toBe(201);
  const other = (await f.client.projects({ projectKey: "OTHER" }).get()).data!;
  expect(
    (
      await f.client.projects({ projectKey: "OTHER" }).issues.post({
        title: "Out of cohort",
        columnId: other.columns.find((c) => c.stateType === "completed")!.id,
      })
    ).status,
  ).toBe(201);
  expect(await report()).toEqual([
    {
      issue_key: "HOME-1",
      runs: 0,
      unmeasured_runs: 0,
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
    },
  ]);
});
