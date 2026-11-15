import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findToast, openApp } from "../../test/app";
import { characters, ids, lobby, members } from "../../test/fixtures";
import { keys } from "../../api/lobby";
import {
  activeSession,
  event,
  sessionId,
  sessionRoutes,
  story,
} from "../../test/fixtures/session";
import { keepDraft } from "../../lib/session-expiry.js";
import { composeMessage } from "../../lib/session-compose.js";

import type { Routes } from "../../test/backend";
import { sessionKeys } from "../../api/session";

const text = "The lock is warm. Keep this between us.";
const requestId = "99999999-9999-4999-8999-999999999999";
const target = { id: story[1]!.id, name: "Brannoch Stone" };
const operation = "POST /campaigns/{campaign_id}/sessions/{session_id}/events";
const recipientChanged =
  "The recipient is no longer available. Your text was kept for the DM. Choose who should receive it before sending.";
const replacement = {
  ...members[1]!,
  id: "77777777-7777-4777-8777-777777777777",
  email: "replacement@example.test",
  player_character_id: ids.spare,
};

afterEach(() => vi.unstubAllGlobals());

function savedReply(
  recipient = ids.spare,
  savedSession: string | null = sessionId,
) {
  const audience = `pc:${recipient}`;
  const message = composeMessage({
    text,
    dm: true,
    audience,
    replyTo: target,
    resolved: true,
    speakerKey: "narrator",
  });
  keepDraft(
    ids.owned,
    { id: lobby().user.id, role: "dm" },
    {
      ...(savedSession ? { sessionId: savedSession } : {}),
      text,
      audience,
      replyTo: target,
      resolved: true,
      speakerKey: "narrator",
      speakerLabel: "",
      pending: {
        id: requestId,
        signature: JSON.stringify({ sessionId, ...message, text }),
      },
    },
  );
}

function open(routes: Routes = {}) {
  return openApp(
    {
      "GET /lobby": lobby(),
      "GET /campaigns/{campaign_id}/characters": characters,
      ...sessionRoutes,
      [operation]: event({ kind: "narration", body: { text } }),
      ...routes,
    },
    `/campaigns/${ids.owned}/session`,
  );
}

describe("kept private replies", () => {
  it("does not save transient draft text when a viewer change removes the loading session", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", {
      origin: "https://friends.example.test",
      reload,
    });
    const { user, backend, client } = open();
    await user.type(
      await screen.findByRole(
        "textbox",
        { name: "Set the scene" },
        { timeout: 2_000 },
      ),
      text,
    );
    const nextSession = "66666666-6666-4666-8666-666666666666";
    let finish!: (value: ReturnType<typeof event>[]) => void;
    const held = new Promise<ReturnType<typeof event>[]>((resolve) => {
      finish = resolve;
    });
    backend.set({
      "GET /campaigns/{campaign_id}/sessions": [
        { ...activeSession, id: nextSession },
      ],
      "GET /campaigns/{campaign_id}/sessions/{session_id}/events": () => held,
    });
    await act(async () => {
      await client.invalidateQueries({
        queryKey: sessionKeys.sessions(ids.owned),
      });
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("textbox", { name: "Set the scene" }),
      ).toBeNull(),
    );
    backend.set({
      "GET /lobby": lobby({
        user: {
          id: "88888888-8888-4888-8888-888888888888",
          email: "replacement@example.test",
          display_name: "Replacement",
        },
      }),
    });
    await act(async () => {
      await client.invalidateQueries({ queryKey: keys.lobby });
      finish([]);
    });
    await screen.findByText("Your account or campaign access changed");
    expect(reload).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(`grimoire-draft:${ids.owned}`)).toBeNull();
    expect(screen.queryByDisplayValue(text)).toBeNull();
  });

  it.each([
    "reassigned",
    "removed",
    "author moved",
    "missing action",
    "deleted author",
    "retracted action",
  ])(
    "keeps a restored reply for the DM when its original recipient is %s",
    async (change) => {
      savedReply();
      const oldAuthor = {
        ...members[1]!,
        player_character_id: change === "author moved" ? ids.mira : null,
      };
      const roster =
        change === "removed"
          ? [members[0]!, replacement]
          : change === "reassigned" || change === "author moved"
            ? [members[0]!, oldAuthor, replacement]
            : [members[0]!, { ...members[1]!, player_character_id: ids.spare }];
      const events = story.flatMap((row) =>
        row.id !== target.id
          ? [row]
          : change === "missing action"
            ? []
            : [
                {
                  ...row,
                  ...(change === "deleted author"
                    ? { author_member_id: null }
                    : {}),
                  ...(change === "retracted action"
                    ? { retracted_at: "2026-11-10T20:00:00Z" }
                    : {}),
                },
              ],
      );
      const { user, backend } = open({
        "GET /campaigns/{campaign_id}/members": roster,
        "GET /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
          events.filter(
            (row) => row.seq > Number(call.query.get("after") ?? 0),
          ),
      });
      const message = await screen.findByRole(
        "region",
        { name: "Message" },
        { timeout: 2_000 },
      );
      await within(message).findByText(recipientChanged);
      expect(
        (screen.getByLabelText("Set the scene") as HTMLTextAreaElement).value,
      ).toBe(text);
      expect(screen.queryByRole("button", { name: "Cancel reply" })).toBeNull();
      expect(
        screen.queryByRole("checkbox", { name: "Mark action resolved" }),
      ).toBeNull();
      expect(
        screen.getByRole("combobox", { name: "Send to" }).textContent,
      ).toBe("DM notes");
      await user.click(screen.getByRole("button", { name: "Send" }));
      const sent = backend.callsTo(operation)[0]!.body as {
        body: object;
        request_id: string;
      };
      expect(sent).toMatchObject({
        audience: "dm",
        audience_pc_ids: [],
        body: { text },
      });
      expect(sent.body).not.toHaveProperty("reply_to");
      expect(sent.body).not.toHaveProperty("resolved");
      expect(sent.request_id).not.toBe(requestId);
    },
  );

  it.each([false, true])(
    "fences a confirmed reassignment while a private reply is open (restored=%s)",
    async (restored) => {
      if (restored) savedReply();
      const { user, backend, client } = open();
      if (restored) await screen.findByRole("button", { name: "Cancel reply" });
      else {
        await user.click(
          await screen.findByRole("button", {
            name: "Reply privately to Brannoch Stone",
          }),
        );
        await user.type(screen.getByLabelText("Set the scene"), text);
      }
      backend.set({
        "GET /campaigns/{campaign_id}/members": [
          members[0]!,
          members[1]!,
          replacement,
        ],
      });
      await act(async () => {
        await client.invalidateQueries({ queryKey: keys.members(ids.owned) });
      });
      await within(screen.getByRole("region", { name: "Message" })).findByText(
        recipientChanged,
      );
      expect(screen.queryByRole("button", { name: "Cancel reply" })).toBeNull();
      expect(
        (screen.getByLabelText("Set the scene") as HTMLTextAreaElement).value,
      ).toBe(text);
      await user.click(screen.getByRole("button", { name: "Send" }));
      expect(backend.callsTo(operation)[0]?.body).toMatchObject({
        audience: "dm",
        body: { text },
      });
      expect(
        (backend.callsTo(operation)[0]!.body as { body: object }).body,
      ).not.toHaveProperty("reply_to");
    },
  );

  it("retains an unchanged request identity when its original action already has a resolved reply", async () => {
    savedReply();
    const landed = event({
      kind: "narration",
      audience: "pcs",
      audience_pc_ids: [ids.spare],
      body: { text, reply_to: target.id, resolved: true },
    });
    const { user, backend } = open({
      "GET /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
        [...story, landed].filter(
          (row) => row.seq > Number(call.query.get("after") ?? 0),
        ),
    });
    await screen.findByRole("button", { name: "Cancel reply" });
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(backend.callsTo(operation)[0]?.body).toMatchObject({
      request_id: requestId,
      body: { reply_to: target.id },
    });
  });

  it("uses a new request after cancelling and reselecting the same restored reply", async () => {
    savedReply();
    const { user, backend } = open();
    await user.click(
      await screen.findByRole("button", { name: "Cancel reply" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Reply privately to Brannoch Stone" }),
    );
    await user.type(screen.getByLabelText("Set the scene"), text);
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(
      (backend.callsTo(operation)[0]!.body as { request_id: string })
        .request_id,
    ).not.toBe(requestId);
  });

  it("keeps a reselected same-target draft when the cancelled send finishes", async () => {
    let finish!: (value: ReturnType<typeof event>) => void;
    const response = new Promise<ReturnType<typeof event>>((resolve) => {
      finish = resolve;
    });
    const { user, backend, client } = open({ [operation]: () => response });
    const reply = await screen.findByRole("button", {
      name: "Reply privately to Brannoch Stone",
    });
    await user.click(reply);
    await user.type(screen.getByLabelText("Set the scene"), text);
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(backend.callsTo(operation)).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Cancel reply" }));
    await user.click(reply);
    await user.type(screen.getByLabelText("Set the scene"), text);
    await act(async () => {
      finish(event({ kind: "narration", body: { text, reply_to: target.id } }));
    });
    await waitFor(() => expect(client.isMutating()).toBe(0));
    expect(
      (screen.getByLabelText("Set the scene") as HTMLTextAreaElement).value,
    ).toBe(text);
    expect(screen.getByRole("button", { name: "Cancel reply" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Send" }));
    const calls = backend.callsTo(operation);
    expect((calls[1]!.body as { request_id: string }).request_id).not.toBe(
      (calls[0]!.body as { request_id: string }).request_id,
    );
  });

  it("keeps unsent text above the loading boundary during an uncached session change", async () => {
    const { user, backend, client } = open();
    await user.click(
      await screen.findByRole("button", {
        name: "Reply privately to Brannoch Stone",
      }),
    );
    await user.type(screen.getByLabelText("Set the scene"), text);
    const nextSession = "66666666-6666-4666-8666-666666666666";
    let finish!: (value: ReturnType<typeof event>[]) => void;
    const response = new Promise<ReturnType<typeof event>[]>((resolve) => {
      finish = resolve;
    });
    backend.set({
      "GET /campaigns/{campaign_id}/sessions": [
        { ...activeSession, id: nextSession },
      ],
      "GET /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
        call.params.session_id === nextSession ? response : [],
    });
    await act(async () => {
      await client.invalidateQueries({
        queryKey: sessionKeys.sessions(ids.owned),
      });
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("textbox", { name: "Set the scene" }),
      ).toBeNull(),
    );
    await act(async () => {
      finish([]);
    });
    const box = await screen.findByRole("textbox", { name: "Set the scene" });
    expect((box as HTMLTextAreaElement).value).toBe(text);
    expect(screen.queryByRole("button", { name: "Cancel reply" })).toBeNull();
    expect(screen.getByRole("combobox", { name: "Send to" }).textContent).toBe(
      "DM notes",
    );
    await user.click(screen.getByRole("button", { name: "Send" }));
    const sent = backend.callsTo(operation)[0]!;
    expect(sent.params.session_id).toBe(nextSession);
    expect(sent.body).toMatchObject({ audience: "dm", body: { text } });
    expect((sent.body as { body: object }).body).not.toHaveProperty("reply_to");
  });

  it("restores the reply, resolution and request id after sign-in", async () => {
    savedReply();
    const { user, backend } = open();
    // The first app mount also loads the lazy session route after identity.
    await waitFor(
      () =>
        expect(
          (screen.getByLabelText("Set the scene") as HTMLTextAreaElement).value,
        ).toBe(text),
      { timeout: 2_000 },
    );
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(backend.callsTo(operation)[0]?.body).toMatchObject({
      audience: "pcs",
      audience_pc_ids: [ids.spare],
      request_id: requestId,
      body: { text, reply_to: target.id, resolved: true },
    });
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Cancel reply" })).toBeNull(),
    );
  });

  it("lets the DM cancel a restored reply without discarding the text", async () => {
    savedReply();
    const { user, backend } = open();
    await user.click(
      await screen.findByRole("button", { name: "Cancel reply" }),
    );
    expect(
      (screen.getByLabelText("Set the scene") as HTMLTextAreaElement).value,
    ).toBe(text);
    await user.click(screen.getByRole("button", { name: "Send" }));
    const sent = backend.callsTo(operation)[0]?.body as {
      body: object;
      request_id: string;
    };
    expect(sent.body).not.toHaveProperty("reply_to");
    expect(sent.request_id).not.toBe(requestId);
  });

  it("does not restore a reply to a character no longer available", async () => {
    savedReply("77777777-7777-4777-8777-777777777777");
    const { user, backend } = open();
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Set the scene") as HTMLTextAreaElement).value,
      ).toBe(text),
    );
    expect(screen.queryByRole("button", { name: "Cancel reply" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Send" }));
    const sent = backend.callsTo(operation)[0]?.body as {
      body: object;
      request_id: string;
      audience: string;
    };
    expect(sent.audience).toBe("dm");
    expect(sent.body).not.toHaveProperty("reply_to");
    expect(sent.request_id).not.toBe(requestId);
  });

  it.each([sessionId, null])(
    "keeps text for the DM when a saved reply belongs to an old or unknown session (%s)",
    async (savedSession) => {
      savedReply(ids.spare, savedSession);
      const nextSession = "66666666-6666-4666-8666-666666666666";
      const { user, backend } = open({
        "GET /campaigns/{campaign_id}/sessions": [
          { ...activeSession, id: nextSession },
        ],
        "GET /campaigns/{campaign_id}/sessions/{session_id}/events": [],
      });
      await waitFor(() =>
        expect(
          (screen.getByLabelText("Set the scene") as HTMLTextAreaElement).value,
        ).toBe(text),
      );
      expect(screen.queryByRole("button", { name: "Cancel reply" })).toBeNull();
      expect(
        screen.queryByRole("checkbox", { name: "Mark action resolved" }),
      ).toBeNull();
      expect(
        screen.getByRole("combobox", { name: "Send to" }).textContent,
      ).toBe("DM notes");
      expect(
        screen.getByText(
          "The session changed. Your text was kept for the DM. Choose who should receive it before sending.",
        ),
      ).toBeTruthy();
      await user.click(screen.getByRole("button", { name: "Send" }));
      const sent = backend.callsTo(operation)[0]!;
      expect(sent.params.session_id).toBe(nextSession);
      expect(sent.body).toMatchObject({
        audience: "dm",
        audience_pc_ids: [],
        body: { text },
      });
      expect((sent.body as { body: object }).body).not.toHaveProperty(
        "reply_to",
      );
      expect((sent.body as { body: object }).body).not.toHaveProperty(
        "resolved",
      );
      expect((sent.body as { request_id: string }).request_id).not.toBe(
        requestId,
      );
    },
  );

  it.each([false, true])(
    "clears a live reply when the mounted composer changes sessions (restored=%s)",
    async (restored) => {
      if (restored) savedReply();
      const { user, backend, client } = open({
        [operation]: { status: 503, detail: "The table is busy." },
      });
      if (!restored) {
        await user.click(
          await screen.findByRole("button", {
            name: "Reply privately to Brannoch Stone",
          }),
        );
        await user.type(screen.getByLabelText("Set the scene"), text);
      } else {
        await screen.findByRole("button", { name: "Cancel reply" });
      }
      await user.click(screen.getByRole("button", { name: "Send" }));
      await findToast("The table is busy.");
      const firstId = (
        backend.callsTo(operation)[0]!.body as { request_id: string }
      ).request_id;
      const nextSession = "66666666-6666-4666-8666-666666666666";
      backend.set({
        "GET /campaigns/{campaign_id}/sessions": [
          { ...activeSession, id: nextSession },
        ],
      });
      // Every session-specific blocking read is cached, so the existing table
      // and composer stay mounted while the selected session changes.
      await act(async () => {
        client.setQueryData(sessionKeys.events(ids.owned, nextSession), []);
        client.setQueryData(sessionKeys.initiative(ids.owned, nextSession), {});
        await client.invalidateQueries({
          queryKey: sessionKeys.sessions(ids.owned),
        });
      });
      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "Cancel reply" }),
        ).toBeNull(),
      );
      expect(
        (screen.getByLabelText("Set the scene") as HTMLTextAreaElement).value,
      ).toBe(text);
      expect(
        screen.getByRole("combobox", { name: "Send to" }).textContent,
      ).toBe("DM notes");
      expect(
        screen.getByText(
          "The session changed. Your text was kept for the DM. Choose who should receive it before sending.",
        ),
      ).toBeTruthy();
      await user.click(screen.getByRole("button", { name: "Send" }));
      const sent = backend.callsTo(operation).at(-1)!;
      expect(sent.params.session_id).toBe(nextSession);
      expect(sent.body).toMatchObject({
        audience: "dm",
        audience_pc_ids: [],
        body: { text },
      });
      expect((sent.body as { body: object }).body).not.toHaveProperty(
        "reply_to",
      );
      expect((sent.body as { body: object }).body).not.toHaveProperty(
        "resolved",
      );
      expect((sent.body as { request_id: string }).request_id).not.toBe(
        firstId,
      );
    },
  );

  it("does not revive a selected reply after an ended session unmounts the composer", async () => {
    const { user, backend, client } = open();
    await user.click(
      await screen.findByRole("button", {
        name: "Reply privately to Brannoch Stone",
      }),
    );
    backend.set({
      "GET /campaigns/{campaign_id}/sessions": [
        { ...activeSession, status: "ended" },
      ],
    });
    await act(async () => {
      await client.invalidateQueries({
        queryKey: sessionKeys.sessions(ids.owned),
      });
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("textbox", { name: "Set the scene" }),
      ).toBeNull(),
    );
    const nextSession = "66666666-6666-4666-8666-666666666666";
    backend.set({
      "GET /campaigns/{campaign_id}/sessions": [
        { ...activeSession, id: nextSession },
      ],
    });
    await act(async () => {
      client.setQueryData(sessionKeys.events(ids.owned, nextSession), []);
      await client.invalidateQueries({
        queryKey: sessionKeys.sessions(ids.owned),
      });
    });
    await screen.findByRole("textbox", { name: "Set the scene" });
    expect(screen.queryByRole("button", { name: "Cancel reply" })).toBeNull();
    await user.type(
      screen.getByRole("textbox", { name: "Set the scene" }),
      text,
    );
    await user.click(screen.getByRole("button", { name: "Send" }));
    const sent = backend.callsTo(operation)[0]!;
    expect(sent.params.session_id).toBe(nextSession);
    expect((sent.body as { body: object }).body).not.toHaveProperty("reply_to");
  });

  it("keeps the new private reply when a send from the unmounted old composer finishes", async () => {
    let finish!: (value: ReturnType<typeof event>) => void;
    const response = new Promise<ReturnType<typeof event>>((resolve) => {
      finish = resolve;
    });
    const { user, backend, client } = open({ [operation]: () => response });
    await user.click(
      await screen.findByRole("button", {
        name: "Reply privately to Brannoch Stone",
      }),
    );
    await user.type(
      screen.getByRole("textbox", { name: "Set the scene" }),
      text,
    );
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(backend.callsTo(operation)).toHaveLength(1));
    backend.set({
      "GET /campaigns/{campaign_id}/sessions": [
        { ...activeSession, status: "ended" },
      ],
    });
    await act(async () => {
      await client.invalidateQueries({
        queryKey: sessionKeys.sessions(ids.owned),
      });
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("textbox", { name: "Set the scene" }),
      ).toBeNull(),
    );
    const nextSession = "66666666-6666-4666-8666-666666666666";
    const nextAction = event({
      kind: "action",
      session_id: nextSession,
      audience: "dm",
      author_member_id: ids.playerMember,
      body: { text: "A new secret." },
    });
    backend.set({
      "GET /campaigns/{campaign_id}/sessions": [
        { ...activeSession, id: nextSession },
      ],
      "GET /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
        [nextAction].filter(
          (row) => row.seq > Number(call.query.get("after") ?? 0),
        ),
    });
    await act(async () => {
      client.setQueryData(sessionKeys.events(ids.owned, nextSession), [
        nextAction,
      ]);
      await client.invalidateQueries({
        queryKey: sessionKeys.sessions(ids.owned),
      });
    });
    await user.click(
      await screen.findByRole("button", {
        name: "Reply privately to Brannoch Stone",
      }),
    );
    await user.type(
      screen.getByRole("textbox", { name: "Set the scene" }),
      "A new private answer.",
    );
    await act(async () => {
      finish(event({ kind: "narration", body: { text } }));
    });
    await waitFor(() => expect(client.isMutating()).toBe(0));
    expect(screen.getByRole("button", { name: "Cancel reply" })).toBeTruthy();
    expect(
      (
        screen.getByRole("textbox", {
          name: "Set the scene",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe("A new private answer.");
    expect(screen.getByRole("combobox", { name: "Send to" }).textContent).toBe(
      "Brannoch Stone and DM",
    );
  });

  it("keeps a different reply with identical text when the earlier send finishes", async () => {
    let finish!: (value: ReturnType<typeof event>) => void;
    const response = new Promise<ReturnType<typeof event>>((resolve) => {
      finish = resolve;
    });
    const nextAction = event({
      kind: "action",
      audience: "dm",
      author_member_id: ids.playerMember,
      body: { text: "Another secret." },
    });
    const { user, backend, client } = open({
      [operation]: () => response,
      "GET /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
        [...story, nextAction].filter(
          (row) => row.seq > Number(call.query.get("after") ?? 0),
        ),
    });
    const replies = await screen.findAllByRole("button", {
      name: "Reply privately to Brannoch Stone",
    });
    await user.click(replies[0]!);
    await user.type(
      screen.getByRole("textbox", { name: "Set the scene" }),
      text,
    );
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(backend.callsTo(operation)).toHaveLength(1));
    await user.click(replies[1]!);
    await user.type(
      screen.getByRole("textbox", { name: "Set the scene" }),
      text,
    );
    await act(async () => {
      finish(
        event({
          kind: "narration",
          body: { text, reply_to: target.id, resolved: true },
        }),
      );
    });
    await waitFor(() => expect(client.isMutating()).toBe(0));
    expect(screen.getByRole("button", { name: "Cancel reply" })).toBeTruthy();
    expect(
      (
        screen.getByRole("textbox", {
          name: "Set the scene",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe(text);
    await user.click(screen.getByRole("button", { name: "Send" }));
    const [first, second] = backend.callsTo(operation);
    expect(second?.body).toMatchObject({
      body: { text, reply_to: nextAction.id, resolved: true },
    });
    expect((second!.body as { request_id: string }).request_id).not.toBe(
      (first!.body as { request_id: string }).request_id,
    );
  });
});
