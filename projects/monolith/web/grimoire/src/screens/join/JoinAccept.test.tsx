import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { OtherRoutes, Routes } from "../../test/backend";
import { findToast, openApp } from "../../test/app";
import { characters, lobby, members } from "../../test/fixtures";
import {
  joinInvitationId,
  joinOther,
  joinState,
} from "../../test/fixtures/join";

const STATE = "GET /grimoire/join/accept/state";
const ACCEPT = "POST /grimoire/join/accept";
const CLOSE = "POST /grimoire/join";

const lobbyRoutes: Routes = {
  "GET /lobby": lobby(),
  "GET /campaigns/{campaign_id}/members": members,
  "GET /campaigns/{campaign_id}/characters": characters,
  "GET /campaigns/{campaign_id}/invitations": [],
  "GET /campaigns/{campaign_id}/join-links": { status: 503 },
};

const open = (other: OtherRoutes = {}) =>
  openApp(lobbyRoutes, "/join/accept", { ...joinOther, ...other });

const lobbyHeading = () =>
  screen.findByRole("heading", { name: "Your campaigns", level: 1 });

describe("JoinAccept", () => {
  it("shows the campaign, both emails and the expiry in UTC", async () => {
    open();
    expect(
      await screen.findByRole("heading", { name: "The Hollow Lantern" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Join a campaign", level: 1 }),
    ).toBeTruthy();
    expect(screen.getByText("Invited email")).toBeTruthy();
    expect(screen.getByText("Signed in as")).toBeTruthy();
    expect(screen.getAllByText("ash@example.test")).toHaveLength(2);
    expect(screen.getByText("18 Nov 2026, 12:00 UTC")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Sign out" })).toBeNull();
  });

  it("accepts the named invitation, then opens a refreshed lobby", async () => {
    const { user, backend } = open();
    await user.click(
      await screen.findByRole("button", { name: "Accept invitation" }),
    );
    expect(await lobbyHeading()).toBeTruthy();
    expect(backend.callsTo(ACCEPT).map((call) => call.body)).toEqual([
      { invitation_id: joinInvitationId },
    ]);
    expect(await findToast("You joined The Hollow Lantern.")).toBeTruthy();
    expect(backend.callsTo("GET /lobby").length).toBeGreaterThan(0);
    expect(backend.callsTo(CLOSE)).toHaveLength(0);
  });

  it("closes the invitation in this browser, then opens the lobby", async () => {
    const { user, backend } = open();
    await user.click(await screen.findByRole("button", { name: "Close" }));
    expect(await lobbyHeading()).toBeTruthy();
    expect(backend.callsTo(CLOSE).map((call) => call.body)).toEqual([
      { action: "cancel" },
    ]);
    expect(backend.callsTo(ACCEPT)).toHaveLength(0);
  });

  it("sends one request however fast the buttons are pressed", async () => {
    const { backend } = open();
    const accept = await screen.findByRole("button", {
      name: "Accept invitation",
    });
    fireEvent.click(accept);
    fireEvent.click(accept);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(await lobbyHeading()).toBeTruthy();
    expect(backend.callsTo(ACCEPT)).toHaveLength(1);
    expect(backend.callsTo(CLOSE)).toHaveLength(0);
  });

  it("shows a refused accept and leaves the page usable", async () => {
    const refusal = "Sign in with the account this invitation was created for.";
    const { user, backend } = open({
      [ACCEPT]: { status: 400, detail: refusal },
    });
    await user.click(
      await screen.findByRole("button", { name: "Accept invitation" }),
    );
    expect(await findToast(refusal)).toBeTruthy();
    // The invitation is read again, in case the refusal means it changed.
    await waitFor(() => expect(backend.callsTo(STATE)).toHaveLength(2));
    expect(
      screen.getByRole("heading", { name: "The Hollow Lantern" }),
    ).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(await lobbyHeading()).toBeTruthy();
  });

  it("warns when the invited email is not the signed-in one", async () => {
    open({
      [STATE]: joinState(
        { invitee_email: "wren@example.test" },
        { matches: false },
      ),
    });
    expect(
      await screen.findByText("The invited email differs from your account"),
    ).toBeTruthy();
    expect(screen.getByText("wren@example.test")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Sign out" }).getAttribute("href"),
    ).toBe("/grimoire/oauth2/logout");
    // The mismatch is advisory: the backend checks identity on accept.
    expect(
      screen.getByRole("button", { name: "Accept invitation" }),
    ).toBeTruthy();
  });

  it("offers only Close once the invitation was accepted", async () => {
    open({ [STATE]: joinState({ status: "accepted" }) });
    expect(
      await screen.findByText("This invitation has already been accepted."),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Accept invitation" }),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "Close" })).toBeTruthy();
  });

  it("names any other status and asks for a new link", async () => {
    open({ [STATE]: joinState({ status: "revoked" }, { matches: false }) });
    expect(
      await screen.findByText(
        "This invitation is revoked. Ask the campaign owner for a new link.",
      ),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Accept invitation" }),
    ).toBeNull();
    expect(screen.queryByRole("link", { name: "Sign out" })).toBeNull();
  });

  it("shows why there is no invitation and how to recover", async () => {
    const message =
      "This invitation link is incomplete. Open the full link shared by the campaign owner.";
    const { user, backend } = open({
      [STATE]: { status: 400, detail: message },
    });
    expect(await screen.findByText(message)).toBeTruthy();
    expect(
      screen.getByText("Open the original invitation again to continue."),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Accept invitation" }),
    ).toBeNull();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(await lobbyHeading()).toBeTruthy();
    expect(backend.callsTo(CLOSE)).toHaveLength(1);
  });

  it("drops a stale invitation when restored from the back/forward cache", async () => {
    let status = "pending";
    const { backend } = open({ [STATE]: () => joinState({ status }) });
    await screen.findByRole("button", { name: "Accept invitation" });
    status = "accepted";
    window.dispatchEvent(new Event("pageshow"));
    expect(backend.callsTo(STATE)).toHaveLength(1);
    window.dispatchEvent(
      Object.assign(new Event("pageshow"), { persisted: true }),
    );
    expect(
      await screen.findByText("This invitation has already been accepted."),
    ).toBeTruthy();
    expect(backend.callsTo(STATE)).toHaveLength(2);
    expect(
      screen.queryByRole("button", { name: "Accept invitation" }),
    ).toBeNull();
  });
});
