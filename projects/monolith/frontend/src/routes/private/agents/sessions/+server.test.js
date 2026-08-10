import { describe, expect, test, vi } from "vitest";
import { POST } from "./+server.js";

describe("sessions proxy", () => {
  test("forwards repo and branch fields in POST body", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ session_id: "123" }), {
          ok: true,
          status: 200,
        }),
    );

    const request = new Request("http://localhost/agents/sessions", {
      method: "POST",
      body: JSON.stringify({
        prompt: "test",
        repo: "owner/repo",
        branch: "main",
      }),
    });

    await POST({ request });

    const callBody = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(callBody.repo).toBe("owner/repo");
    expect(callBody.branch).toBe("main");
    expect(callBody.prompt).toBe("test");
  });

  test("omits repo field when not provided", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ session_id: "123" }), {
          ok: true,
          status: 200,
        }),
    );

    const request = new Request("http://localhost/agents/sessions", {
      method: "POST",
      body: JSON.stringify({
        prompt: "test",
        workspace: "ws",
      }),
    });

    await POST({ request });

    const callBody = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(callBody.repo).toBeUndefined();
    expect(callBody.workspace).toBe("ws");
  });

  test("forwards x-auth-email header when present", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ session_id: "123" }), {
          ok: true,
          status: 200,
        }),
    );

    const request = new Request("http://localhost/agents/sessions", {
      method: "POST",
      body: JSON.stringify({ prompt: "test" }),
      headers: { "X-Auth-Email": "human@example.com" },
    });

    await POST({ request });

    expect(global.fetch.mock.calls[0][1].headers["X-Auth-Email"]).toBe(
      "human@example.com",
    );
  });

  test("omits x-auth-email header when absent", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ session_id: "123" }), {
          ok: true,
          status: 200,
        }),
    );

    const request = new Request("http://localhost/agents/sessions", {
      method: "POST",
      body: JSON.stringify({ prompt: "test" }),
    });

    await POST({ request });

    expect(
      global.fetch.mock.calls[0][1].headers["X-Auth-Email"],
    ).toBeUndefined();
  });

  test("includes model field when provided", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ session_id: "123" }), {
          ok: true,
          status: 200,
        }),
    );

    const request = new Request("http://localhost/agents/sessions", {
      method: "POST",
      body: JSON.stringify({
        prompt: "test",
        model: "opus",
      }),
    });

    await POST({ request });

    const callBody = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(callBody.model).toBe("opus");
  });
});
