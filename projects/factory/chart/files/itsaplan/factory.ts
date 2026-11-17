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
  getIssue,
  getIssueProjectId,
  updateIssue,
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
  );
