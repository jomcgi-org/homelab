import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Routes } from "../../test/backend";
import { findToast, openApp } from "../../test/app";
import {
  campaigns,
  characters,
  ids,
  lobby,
  members,
} from "../../test/fixtures";
import {
  activeSession,
  event,
  sessionId,
  sessionRoutes,
  story,
} from "../../test/fixtures/session";

const path = `/campaigns/${ids.owned}/session`;
const dm: Routes = {
  "GET /lobby": lobby(),
  "GET /campaigns/{campaign_id}/characters": characters,
  ...sessionRoutes,
};
const player: Routes = {
  ...dm,
  "GET /lobby": lobby({
    campaigns: [
      {
        ...campaigns.owned,
        role: "player",
        is_owner: false,
        player_character_id: ids.spare,
        character_name: "Brannoch Stone",
      },
    ],
  }),
};

describe("Session", () => {
  it("shows the story with who can see each entry", async () => {
    openApp(dm, path);
    const feed = await screen.findByRole("list", { name: "Session feed" });
    expect(
      within(feed).getByText("Rain hammers the shutters of the Gilded Eel."),
    ).toBeTruthy();
    expect(within(feed).getByText("17")).toBeTruthy();
    expect(within(feed).getByText("Perception · 1d20+3")).toBeTruthy();
    expect(
      within(feed).getByText("Turn order set. Round 1: Brannoch Stone's turn."),
    ).toBeTruthy();
    expect(within(feed).getByText("Waiting for DM")).toBeTruthy();
    expect(screen.getByText("Live")).toBeTruthy();
    // Each entry is a real list item, so a screen reader announces the count.
    expect(within(feed).getAllByRole("listitem")).toHaveLength(story.length);
  });

  it("asks only for events after the last one it holds", async () => {
    const { backend } = openApp(dm, path);
    await screen.findByRole("list", { name: "Session feed" });
    const calls = () =>
      backend.callsTo(
        "GET /campaigns/{campaign_id}/sessions/{session_id}/events",
      );
    await waitFor(() => expect(calls().length).toBeGreaterThan(1), {
      timeout: 4000,
    });
    expect(calls()[0]!.query.get("after")).toBe("0");
    expect(calls().at(-1)!.query.get("after")).toBe(String(story.at(-1)!.seq));
  });

  it("re-reads the whole feed as soon as a reveal arrives", async () => {
    // A reveal can retract knowledge already shown, which edits old events.
    const reveal = event({ kind: "reveal", body: { retracted: true } });
    let delivered = false;
    const { backend } = openApp(
      {
        ...dm,
        "GET /campaigns/{campaign_id}/sessions/{session_id}/events": (call) => {
          if (call.query.get("after") === "0")
            return delivered ? [...story, reveal] : story;
          if (delivered) return [];
          delivered = true;
          return [reveal];
        },
      },
      path,
    );
    await screen.findByRole("list", { name: "Session feed" });
    const fullReads = () =>
      backend
        .callsTo("GET /campaigns/{campaign_id}/sessions/{session_id}/events")
        .filter((call) => call.query.get("after") === "0");
    // Well inside the periodic full read's fifteen seconds.
    await waitFor(() => expect(fullReads()).toHaveLength(2), { timeout: 4000 });
  });

  it("sends narration to everyone with a request id", async () => {
    const { user, backend } = openApp(
      {
        ...dm,
        "POST /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
          event({
            kind: "narration",
            body: (call.body as { body: object }).body as never,
          }),
      },
      path,
    );
    await user.type(
      await screen.findByLabelText("Set the scene"),
      "A hooded figure waves you over.",
    );
    await user.click(screen.getByRole("button", { name: "Send" }));
    const [call] = backend.callsTo(
      "POST /campaigns/{campaign_id}/sessions/{session_id}/events",
    );
    expect(call?.params.session_id).toBe(sessionId);
    const sent = call?.body as Record<string, unknown>;
    expect(sent).toMatchObject({
      kind: "narration",
      audience: "table",
      audience_pc_ids: [],
      body: {
        text: "A hooded figure waves you over.",
        speaker_key: "narrator",
      },
    });
    expect(sent.request_id).toMatch(/^[0-9a-f-]{36}$/);
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Set the scene") as HTMLTextAreaElement).value,
      ).toBe(""),
    );
  });

  it("clears the box when the sent text had surrounding whitespace", async () => {
    const { user } = openApp(
      {
        ...dm,
        "POST /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
          event({
            kind: "narration",
            body: (call.body as { body: object }).body as never,
          }),
      },
      path,
    );
    const box = await screen.findByLabelText("Set the scene");
    await user.type(box, "The door creaks. ");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(""));
  });

  it("keeps the draft and reuses the request id when a send fails", async () => {
    const { user, backend } = openApp(
      {
        ...dm,
        "POST /campaigns/{campaign_id}/sessions/{session_id}/events": {
          status: 503,
          detail: "The table is busy.",
        },
      },
      path,
    );
    const box = await screen.findByLabelText("Set the scene");
    await user.type(box, "The door creaks.");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await findToast("The table is busy.")).toBeTruthy();
    expect((box as HTMLTextAreaElement).value).toBe("The door creaks.");
    await user.click(screen.getByRole("button", { name: "Send" }));
    const ids = backend
      .callsTo("POST /campaigns/{campaign_id}/sessions/{session_id}/events")
      .map((call) => (call.body as { request_id: string }).request_id);
    expect(ids).toHaveLength(2);
    expect(ids[0]).toBe(ids[1]);
  });

  it("lets the DM reply privately to a waiting action", async () => {
    const { user, backend } = openApp(
      {
        ...dm,
        "POST /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
          event({
            kind: "narration",
            body: (call.body as { body: object }).body as never,
          }),
      },
      path,
    );
    await user.click(
      await screen.findByRole("button", {
        name: "Reply privately to Brannoch Stone",
      }),
    );
    expect(
      screen.getByText("Replying privately to Brannoch Stone"),
    ).toBeTruthy();
    await user.type(screen.getByLabelText("Set the scene"), "A cold draught.");
    await user.click(screen.getByRole("button", { name: "Send" }));
    const sent = backend.callsTo(
      "POST /campaigns/{campaign_id}/sessions/{session_id}/events",
    )[0]?.body as Record<string, unknown>;
    expect(sent).toMatchObject({
      audience: "pcs",
      audience_pc_ids: [ids.spare],
      body: { reply_to: story[1]!.id, resolved: true },
    });
  });

  it("posts a player action and offers no DM controls", async () => {
    const { user, backend } = openApp(
      {
        ...player,
        "POST /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
          event({
            kind: "action",
            body: (call.body as { body: object }).body as never,
          }),
      },
      path,
    );
    await user.type(
      await screen.findByLabelText("What do you do?"),
      "I listen.",
    );
    await user.click(screen.getByRole("button", { name: "Send" }));
    const sent = backend.callsTo(
      "POST /campaigns/{campaign_id}/sessions/{session_id}/events",
    )[0]?.body as Record<string, unknown>;
    expect(sent).toMatchObject({ kind: "action", audience: "table" });
    expect(sent.body as object).not.toHaveProperty("speaker_key");
    expect(screen.queryByRole("button", { name: "Pause session" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Reply privately/ }),
    ).toBeNull();
  });

  it("rolls a quick die on the server with the chosen mode", async () => {
    const { user, backend } = openApp(
      {
        ...dm,
        "POST /campaigns/{campaign_id}/sessions/{session_id}/rolls": event({
          kind: "roll",
          body: { total: 9, formula: "1d20adv", rolls: [9, 4], kept: [9] },
        }),
      },
      path,
    );
    await user.click(await screen.findByRole("button", { name: "Roll dice" }));
    await user.click(await screen.findByRole("radio", { name: "Advantage" }));
    await user.click(screen.getByRole("button", { name: "d20" }));
    expect(
      backend.callsTo(
        "POST /campaigns/{campaign_id}/sessions/{session_id}/rolls",
      )[0]?.body,
    ).toEqual({ formula: "1d20adv", label: "", visibility: "dm" });
  });

  it("sends with Ctrl+Enter and leaves a plain Enter as a new line", async () => {
    const { user, backend } = openApp(
      {
        ...dm,
        "POST /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
          event({
            kind: "narration",
            body: (call.body as { body: object }).body as never,
          }),
      },
      path,
    );
    const box = await screen.findByLabelText("Set the scene");
    await user.type(box, "First line{Enter}second");
    const posts = () =>
      backend.callsTo(
        "POST /campaigns/{campaign_id}/sessions/{session_id}/events",
      );
    expect(posts()).toHaveLength(0);
    await user.type(box, "{Control>}{Enter}{/Control}");
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect((posts()[0]!.body as { body: { text: string } }).body.text).toBe(
      "First line\nsecond",
    );
  });

  it("confirms before the DM ends the session", async () => {
    const { user, backend } = openApp(
      {
        ...dm,
        "PATCH /campaigns/{campaign_id}/sessions/{session_id}": {
          ...activeSession,
          status: "ended",
        },
      },
      path,
    );
    await user.click(
      await screen.findByRole("button", { name: "End session" }),
    );
    const patch =
      "PATCH /campaigns/{campaign_id}/sessions/{session_id}" as const;
    expect(backend.callsTo(patch)).toHaveLength(0);
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "End session",
      }),
    );
    expect(backend.callsTo(patch)[0]?.body).toEqual({ status: "ended" });
  });

  it("starts the first session", async () => {
    const { user, backend } = openApp(
      {
        ...dm,
        "GET /campaigns/{campaign_id}/sessions": [],
        "POST /campaigns/{campaign_id}/sessions": activeSession,
      },
      path,
    );
    await user.click(
      await screen.findByRole("button", { name: "Start session" }),
    );
    expect(
      backend.callsTo("POST /campaigns/{campaign_id}/sessions"),
    ).toHaveLength(1);
  });

  it("refuses a campaign the viewer does not belong to", async () => {
    openApp({ ...dm, "GET /lobby": lobby({ campaigns: [] }) }, path);
    expect(
      await screen.findByText("You are not a member of this campaign"),
    ).toBeTruthy();
    void members;
  });
});
