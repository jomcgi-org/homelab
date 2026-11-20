// invariant: FACT-13
// Narrow, authenticated extension to the pinned itsaplan v1.10.0 API. Use the
// upstream services so activity, status history, notifications, schedules and
// webhook outboxes retain the same behavior as its ordinary routes.
import { Elysia, t } from "elysia";
import { and, eq, sql } from "drizzle-orm";
import { db, agentRun, aiAgent, issue, label, projectColumn } from "@repo/db";
import { factoryTransaction } from "../../../packages/db/src/client";
import { guards, entityGuard } from "./shared/guards";
import { authContext } from "./shared/auth-context";
import { HttpError, errorAnswer } from "./shared/lib";
import {
  createIssue,
  getIssue,
  getIssueProjectId,
  updateIssue,
  restoreIssue,
} from "./modules/issues/service";
import { createComment } from "./modules/issues/activity";
import { getAssignTriggerAgent } from "./modules/agents/core/service";
import { startDeferred, type DeferredStart as Start } from "./factory-deferred";
import pkg from "../../../package.json";

if (pkg.version !== "1.10.0") {
  throw new Error(
    "Revalidate the deferred transaction adapter before upgrading itsaplan",
  );
}

// Startup is serialized across replicas. Upstream migrations run first in the
// init container. No additional database credential is given to the connector.
await db.transaction(async (tx) => {
  await tx.execute(sql`select pg_advisory_xact_lock(118, 118)`);
  await tx.execute(sql`
    create table if not exists factory_review_followup (
      project_id integer not null,
      finding_key text not null,
      issue_id integer not null,
      primary key (project_id, finding_key)
    )
  `);
  await tx.execute(sql`
    create table if not exists factory_deferred_start (
      issue_id integer not null references issue(id) on delete cascade,
      start_date date not null,
      actor_user_id text not null,
      delegate_user_id text not null,
      run_id integer not null,
      primary key (issue_id, start_date)
    )
  `);
});

const conflict = (message: string) => new HttpError(409, message);
const api = {
  transaction: factoryTransaction,
  conflict,
  async lockIssue(id: number) {
    await db
      .select({ id: issue.id })
      .from(issue)
      .where(eq(issue.id, id))
      .for("update");
  },
  async receipt(r: Start) {
    const rows = await db.execute<{
      actorUserId: string;
      delegateUserId: string;
      runId: number;
    }>(sql`
      select actor_user_id as "actorUserId", delegate_user_id as "delegateUserId", run_id as "runId"
      from factory_deferred_start where issue_id = ${r.issueId} and start_date = ${r.startDate}::date
    `);
    return rows[0];
  },
  async hasRun(id: number, r: Start) {
    const rows = await db
      .select({ id: agentRun.id })
      .from(agentRun)
      .innerJoin(aiAgent, eq(aiAgent.id, agentRun.agentId))
      .where(
        and(
          eq(agentRun.id, id),
          eq(agentRun.issueId, r.issueId),
          eq(agentRun.projectId, r.projectId),
          eq(aiAgent.userId, r.delegateUserId),
          eq(agentRun.trigger, "delegation"),
        ),
      );
    return rows.length === 1;
  },
  async eligible(r: Start) {
    const current = await getIssue(r.issueId);
    const columns = await db
      .select()
      .from(projectColumn)
      .where(eq(projectColumn.projectId, r.projectId));
    const labels = await db
      .select()
      .from(label)
      .where(and(eq(label.projectId, r.projectId), eq(label.name, "deferred")));
    const backlog = columns.find(
      (c) => c.name === "Backlog" && c.stateType === "backlog",
    );
    const todo = columns.find(
      (c) => c.name === "Todo" && c.stateType === "unstarted",
    );
    if (
      !current ||
      !backlog ||
      !todo ||
      current.projectId !== r.projectId ||
      current.columnId !== backlog.id ||
      current.delegateUserId !== null ||
      current.archivedAt !== null ||
      current.startDate !== r.startDate ||
      r.startDate > new Date().toISOString().slice(0, 10) ||
      !labels.some((l) => current.labelIds.includes(l.id))
    ) {
      throw conflict("Issue is no longer eligible for its deferred start");
    }
    const agent = await getAssignTriggerAgent(
      r.projectId,
      r.delegateUserId,
      r.actorUserId,
    );
    if (!agent || r.delegateUserId === r.actorUserId) {
      throw conflict(
        "Coding agent does not accept delegation from this coordinator",
      );
    }
    return { todoId: todo.id, agentId: agent.id };
  },
  comment: createComment,
  update: updateIssue,
  async runs(issueId: number, agentId: number) {
    return (
      await db
        .select({ id: agentRun.id })
        .from(agentRun)
        .where(
          and(
            eq(agentRun.issueId, issueId),
            eq(agentRun.agentId, agentId),
            eq(agentRun.trigger, "delegation"),
          ),
        )
    ).map((row) => row.id);
  },
  async saveReceipt(r: Start, runId: number) {
    await db.execute(sql`
      insert into factory_deferred_start (issue_id, start_date, actor_user_id, delegate_user_id, run_id)
      values (${r.issueId}, ${r.startDate}::date, ${r.actorUserId}, ${r.delegateUserId}, ${runId})
    `);
  },
};

export const factoryRoutes = new Elysia({ name: "factory-deferred" })
  .use(authContext)
  .use(guards)
  .onError(({ code, error, set }) => {
    const answer = errorAnswer(code, error);
    set.status = answer.status;
    return answer.body;
  })
  .macro({
    workItem: entityGuard("work_items", "Issue not found", (p) =>
      getIssueProjectId(Number(p.issueId)),
    ),
  })
  .post(
    "/projects/:projectKey/factory-review-followups",
    async ({ project, user, body }) =>
      factoryTransaction(async () => {
        const coordinator = process.env.FACTORY_COORDINATOR;
        const actor = user!.id;
        if (!coordinator)
          throw new HttpError(403, "Factory coordinator is not configured");
        const authorized = await db.execute(sql`
          select a.user_id from ai_agent a join project p on p.team_id = a.team_id
          where p.id = ${project.id} and a.username = ${coordinator} and a.user_id = ${actor}
        `);
        if (!authorized.length)
          throw new HttpError(
            403,
            "Only the configured coordinator can file review follow-ups",
          );
        const { key, ...input } = body;
        await db.execute(
          sql`select pg_advisory_xact_lock(${project.id}::integer, hashtext(${key}))`,
        );
        const rows = await db.execute<{ id: number }>(sql`
        select issue_id as id from factory_review_followup
        where project_id = ${project.id} and finding_key = ${key}
      `);
        if (rows[0]) return rows[0];
        const created = await createIssue(project, input, user!.id);
        await db.execute(sql`
        insert into factory_review_followup (project_id, finding_key, issue_id)
        values (${project.id}, ${key}, ${created.id})
      `);
        return { id: created.id };
      }),
    {
      permission: ["work_items", "create"],
      body: t.Object({
        key: t.String({ minLength: 1, maxLength: 2048 }),
        title: t.String({ minLength: 1 }),
        description: t.String(),
        columnId: t.Integer({ minimum: 1 }),
      }),
    },
  )
  .post(
    "/issues/:issueId/factory-deferred-start",
    async ({ params, body, user, projectId }) => {
      if (projectId !== body.projectId) throw conflict("Issue changed project");
      return startDeferred(api, {
        ...body,
        projectId,
        issueId: params.issueId,
        actorUserId: user!.id,
        key: `deferred/${params.issueId}/${body.startDate}`,
      });
    },
    {
      workItem: "edit",
      params: t.Object({ issueId: t.Numeric({ minimum: 1 }) }),
      body: t.Object({
        projectId: t.Integer({ minimum: 1 }),
        startDate: t.String({ format: "date" }),
        delegateUserId: t.String({ minLength: 1 }),
      }),
    },
  )
  .patch(
    "/issues/:issueId/factory-semgrep",
    async ({ params, body, user, projectId }) => {
      if (process.env.FACTORY_INTSEC_SYNC !== "true")
        throw new HttpError(404, "Sync disabled");
      const id = params.issueId;
      const actor = user!.id;
      const marker = `Semgrep finding ID: ${body.finding}\n`;
      return factoryTransaction(async () => {
        const owned = await db.execute(sql`
          select i.id from issue i
          join project p on p.id = i.project_id
          join ai_agent a on a.team_id = p.team_id and a.username = 'intsec-sync'
          join issue_activity h on h.issue_id = i.id and h.kind = 'activity' and h.action = 'created'
          join issue_label il on il.issue_id = i.id
          join label l on l.id = il.label_id and l.project_id = p.id
          where i.id = ${id} and p.id = ${projectId} and p.key = 'INTSEC'
            and a.user_id = ${actor} and h.actor_user_id = a.user_id
            and l.name = 'semgrep' and starts_with(i.description, ${marker})
          for update of i, il, l
        `);
        if (!owned.length)
          throw new HttpError(403, "Finding is not owned by sync");
        if (
          body.description !== undefined &&
          !body.description.startsWith(marker)
        )
          throw conflict("Finding identity changed");
        if (body.columnId !== undefined) {
          const column = await db.execute(sql`
            select id from project_column where id = ${body.columnId}
            and project_id = ${projectId} and state_type in ('backlog','completed') for share
          `);
          if (!column.length) throw conflict("Invalid finding destination");
          // A column move can run the project's subtask automations, which
          // move the parent or subtasks without this route's ownership check.
          // So a finding linked to another issue is never moved here.
          const related = await db.execute(sql`
            select 1 from issue
            where (id = ${id} and parent_id is not null) or parent_id = ${id}
            limit 1
          `);
          if (related.length)
            throw conflict("Finding has a parent or subtasks");
        }
        // Upstream's single-row service leaves archived subtasks untouched.
        if (body.restore) await restoreIssue(id, actor);
        const patch = {
          ...(body.title !== undefined && { title: body.title }),
          ...(body.description !== undefined && {
            description: body.description,
          }),
          ...(body.columnId !== undefined && { columnId: body.columnId }),
        };
        if (Object.keys(patch).length) await updateIssue(id, patch, actor);
        return { ok: true };
      });
    },
    {
      workItem: "create",
      params: t.Object({ issueId: t.Numeric({ minimum: 1 }) }),
      body: t.Object({
        finding: t.String({
          pattern: "^((sast|sca)/[0-9]+|secrets/[A-Za-z0-9._~%-]+)$",
        }),
        title: t.Optional(t.String()),
        description: t.Optional(t.String()),
        columnId: t.Optional(t.Integer()),
        restore: t.Optional(t.Boolean()),
      }),
    },
  );
