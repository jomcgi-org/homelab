import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Routes } from "../../test/backend";
import { openApp } from "../../test/app";
import { campaigns, characters, ids, lobby } from "../../test/fixtures";
import {
  campaignJournal,
  journalIds,
  journalRoutes,
  sessionJournal,
} from "../../test/fixtures/journal";

const path = `/campaigns/${ids.owned}/journal`;
const JOURNAL = "GET /campaigns/{campaign_id}/journal";
const dm: Routes = {
  "GET /lobby": lobby(),
  "GET /campaigns/{campaign_id}/characters": characters,
  ...journalRoutes,
};
const player: Routes = {
  ...dm,
  "GET /lobby": lobby({
    campaigns: [{ ...campaigns.owned, role: "player", is_owner: false }],
  }),
};

describe("CampaignJournal", () => {
  it("shows each session's journal under the campaign's name", async () => {
    const { backend } = openApp(dm, path);
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "The Sunless Citadel journal",
      }),
    ).toBeTruthy();
    const sessions = await screen.findAllByRole("region", {
      name: /^Session 2026/,
    });
    expect(sessions).toHaveLength(2);
    expect(
      within(sessions[0]!).getByRole("heading", { level: 3, name: "Learned" }),
    ).toBeTruthy();
    expect(
      within(sessions[0]!).getByRole("heading", { level: 4, name: "Old Tam" }),
    ).toBeTruthy();
    // A session with nothing recorded still shows every section, empty.
    expect(within(sessions[1]!).getByText("No discoveries yet.")).toBeTruthy();
    expect(backend.callsTo(JOURNAL)[0]?.query.get("view")).toBe("mine");
    expect(backend.callsTo(JOURNAL)[0]?.query.has("cursor")).toBe(false);
    // One audience control for the page, none per session.
    expect(screen.getAllByRole("radiogroup")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /in story/ })).toBeNull();
    expect(
      screen.getByRole("link", { name: "Session" }).getAttribute("href"),
    ).toBe(`/campaigns/${ids.owned}/session`);
  });

  it("asks for the party view when it is chosen", async () => {
    const { user, backend } = openApp(player, path);
    await user.click(await screen.findByRole("radio", { name: "Party" }));
    await waitFor(() =>
      expect(backend.callsTo(JOURNAL).at(-1)?.query.get("view")).toBe("party"),
    );
  });

  it("says so when no session has been recorded", async () => {
    openApp({ ...dm, [JOURNAL]: { sessions: [], next_cursor: null } }, path);
    expect(await screen.findByText("No sessions recorded yet.")).toBeTruthy();
  });

  it("loads older sessions after the ones shown", async () => {
    const older = {
      session_id: journalIds.earlierSession,
      started_at: "2026-10-27T19:00:00Z",
      journal: { received: sessionJournal.received },
    };
    const { user, backend } = openApp(
      {
        ...dm,
        [JOURNAL]: (call) =>
          call.query.get("cursor") === "next-page"
            ? { sessions: [older], next_cursor: null }
            : {
                sessions: [campaignJournal.sessions[0]!],
                next_cursor: "next-page",
              },
      },
      path,
    );
    await user.click(
      await screen.findByRole("button", { name: "Older sessions" }),
    );
    expect(
      await screen.findByRole("region", {
        name: "Session 2026-10-27T19:00:00Z",
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("region", { name: "Session 2026-11-10T19:00:00Z" }),
    ).toBeTruthy();
    expect(backend.callsTo(JOURNAL).at(-1)?.query.get("view")).toBe("mine");
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Older sessions" }),
      ).toBeNull(),
    );
  });

  it("fails closed: a refused journal is an error, never an empty one", async () => {
    openApp(
      { ...dm, [JOURNAL]: { status: 403, detail: "Play is disabled." } },
      path,
    );
    expect(await screen.findByText("Could not load the journal")).toBeTruthy();
    expect(screen.getByText("Play is disabled.")).toBeTruthy();
    expect(screen.queryByText("No sessions recorded yet.")).toBeNull();
  });

  it("does not read the journal of a campaign the viewer is not in", async () => {
    const { backend } = openApp(
      dm,
      "/campaigns/77777777-7777-4777-8777-777777777777/journal",
    );
    expect(
      await screen.findByText("You are not a member of this campaign"),
    ).toBeTruthy();
    expect(backend.callsTo(JOURNAL)).toHaveLength(0);
  });
});
