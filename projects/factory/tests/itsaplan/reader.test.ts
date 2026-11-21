import { expect, test } from "bun:test";
import { spawn, execFileSync } from "node:child_process";
import { createInterface } from "node:readline";
import { agentFieldTrigger, agentSchedule, db, project } from "@repo/db";
import { eq } from "drizzle-orm";
import { app, authedApi, createProject } from "./__tests__/helpers/app";
import { createAgent, setAgentProjectRole } from "./__tests__/helpers/agents";
import { signUpTestUser } from "./__tests__/helpers/auth";
import { resetDb } from "./__tests__/helpers/db";

test("Codex discovers tracker tools and the reader can only read issue evidence", async () => {
  await resetDb();
  const owner = authedApi((await signUpTestUser()).cookie);
  const created = (
    await createProject(owner, { key: "READ", name: "Reader fixture" })
  ).data!;
  await db
    .update(project)
    .set({ mcpEnabled: true })
    .where(eq(project.id, created.id));
  const scope = owner.projects({ projectKey: "READ" });
  const columnId = (await scope.get()).data!.columns[0].id;
  const seed = async (title: string) =>
    (await scope.issues.post({ title, columnId })).data!.id;
  const issueId = await seed("Source");
  const targetIssueId = await seed("Evidence");
  const issue = owner.issues({ issueId });
  expect(
    (await issue.links.post({ targetIssueId, kind: "blocks" })).status,
  ).toBe(201);
  const reader = (
    await createAgent(owner, "READ", {
      username: "reader",
      name: "Reader",
      kind: "external",
      triggerOnMention: false,
      triggerOnAssign: false,
    })
  ).data!;
  const role = (
    await owner.teams({ teamId: created.teamId }).roles.post({
      name: "Reader",
      permissions: { work_items: { read: true } },
    })
  ).data!;
  expect(
    (await setAgentProjectRole(owner, "READ", reader.agent.userId, role.id))
      .status,
  ).toBe(204);
  expect(reader.agent.triggerOnMention).toBe(false);
  expect(reader.agent.triggerOnAssign).toBe(false);
  for (const table of [agentFieldTrigger, agentSchedule]) {
    expect(
      await db.select().from(table).where(eq(table.agentId, reader.agent.id)),
    ).toHaveLength(0);
  }
  expect((await issue.comments.post({ body: "Fixture evidence" })).status).toBe(
    201,
  );
  const http = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch: app.handle });
  const env = {
    PATH: process.env.PATH,
    HOME: process.env.READER_HOME,
    CODEX_HOME: process.env.READER_HOME,
    FIXTURE_READER: reader.apiKey!,
  };
  const binary = process.env.READER_CODEX!;
  try {
    execFileSync(
      binary,
      [
        "mcp",
        "add",
        "tracker",
        "--url",
        new URL("/mcp", http.url).href,
        "--bearer-token-env-var",
        "FIXTURE_READER",
      ],
      { env, stdio: "ignore" },
    );
    const proc = spawn(binary, ["app-server"], {
      env,
      stdio: ["pipe", "pipe", "ignore"],
    });
    const lines = createInterface({ input: proc.stdout })[
      Symbol.asyncIterator
    ]();
    let id = 0;
    async function rpc(method: string, params: object) {
      proc.stdin.write(JSON.stringify({ method, params, id: ++id }) + "\n");
      while (true) {
        const line = await lines.next();
        if (line.done) throw Error("Codex app-server closed");
        const message = JSON.parse(line.value);
        if (message.id !== id) continue;
        if (message.error)
          throw Error(method + " failed: " + message.error.message);
        return message.result;
      }
    }
    try {
      await rpc("initialize", {
        clientInfo: { name: "fixture", version: "1" },
        capabilities: { experimentalApi: true },
      });
      proc.stdin.write('{"method":"initialized"}\n');
      const threadId = (await rpc("thread/start", { ephemeral: true })).thread
        .id;
      const { data } = await rpc("mcpServerStatus/list", {
        threadId,
        serverName: "tracker",
      });
      expect(
        data.find((s: { name: string }) => s.name === "tracker")?.tools,
      ).toHaveProperty("get_issue");
      const call = (tool: string, args: object) =>
        rpc("mcpServer/tool/call", {
          threadId,
          server: "tracker",
          tool,
          arguments: args,
        });
      const read = await call("get_issue", { issueId });
      expect(read.structuredContent).toMatchObject({
        ok: true,
        data: { title: "Source", links: [{ issue: { id: targetIssueId } }] },
      });
      expect(
        (await call("get_issue", { issueId: targetIssueId })).structuredContent,
      ).toMatchObject({ ok: true, data: { title: "Evidence" } });
      expect(
        (await call("list_issue_activity", { issueId })).structuredContent.data
          .items,
      ).toContainEqual(
        expect.objectContaining({ kind: "comment", body: "Fixture evidence" }),
      );
      expect(
        (await call("add_comment", { issueId, body: "Forbidden" }))
          .structuredContent,
      ).toMatchObject({ ok: false, status: 403 });
    } finally {
      proc.kill();
    }
  } finally {
    http.stop(true);
  }
}, 60000);
