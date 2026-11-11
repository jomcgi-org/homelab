import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { findToast, openApp } from "../../test/app";
import type { Call, OtherRoutes } from "../../test/backend";
import {
  platformIds,
  platformInvitation,
  platformInvitations,
  platformOther,
  platformToken,
  platformUsers,
} from "../../test/fixtures/platform";

const USERS = "GET /api/auth/platform/management/users";
const INVITATIONS = "GET /api/auth/platform/management/invitations";
const INVITATION = "GET /api/auth/platform/management/invitation";
const COMMANDS = "POST /api/auth/platform/commands";
const DELIVER = "POST /api/auth/platform/invitations/{invitation_id}/deliver";

const open = (other: OtherRoutes = {}, path = "/platform") =>
  openApp({}, path, { ...platformOther, ...other });

const body = (call: Call | undefined) =>
  call?.body as {
    action?: string;
    request_id: string;
    reason: string;
    reissue?: boolean;
    arguments?: Record<string, unknown>;
  };

// findToast also fits a field error: it too is announced, so renders twice.
async function manage(
  user: ReturnType<typeof open>["user"],
  name: string,
  reason = "Agreed at the table",
) {
  await user.click(await screen.findByRole("button", { name }));
  const dialog = await screen.findByRole("dialog");
  await user.type(within(dialog).getByLabelText(/^Reason/), reason);
  return dialog;
}

describe("Platform accounts", () => {
  it("lists invitations and users with their state", async () => {
    open();
    const invitations = await screen.findByRole("region", {
      name: "Invitations",
    });
    expect(
      within(invitations).getByText(
        "Link not shown yet · Expires 18 Nov 2026, 12:00 UTC",
      ),
    ).toBeTruthy();
    expect(within(invitations).getByText(/^Link shown ·/)).toBeTruthy();
    expect(within(invitations).getByText(/^Accepted ·/)).toBeTruthy();
    const users = screen.getByRole("region", { name: "Users" });
    expect(
      within(users).getByText(
        "Active · Grants: grimoire.access, grimoire.create_game",
      ),
    ).toBeTruthy();
    expect(within(users).getByText("Disabled · Grants: None")).toBeTruthy();
    expect(screen.queryByText(/first 50 entries/)).toBeNull();
  });

  it("offers no action on an invitation that is already used", async () => {
    open();
    await screen.findByRole("button", {
      name: "Manage invitation for Juniper",
    });
    expect(
      screen.queryByRole("button", { name: "Manage invitation for Tansy" }),
    ).toBeNull();
  });

  it("says when the first page is not everything", async () => {
    open({ [USERS]: { items: platformUsers, next: platformIds.disabled } });
    expect(await screen.findByText(/first 50 entries/)).toBeTruthy();
  });

  it("is unavailable while management is switched off", async () => {
    open({
      [USERS]: { status: 404 },
      [INVITATIONS]: { status: 404 },
      "GET /api/auth/platform/management/permissions": { status: 404 },
    });
    expect(
      await screen.findByText("Account management is unavailable."),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Import/ })).toBeNull();
  });

  it("asks a refused operator to import their identity", async () => {
    const { user, backend } = open({
      [USERS]: { status: 403, detail: "Explicit bootstrap required." },
    });
    const submit = await screen.findByRole("button", {
      name: "Import my operator identity",
    });
    expect(screen.queryByRole("heading", { name: "Users" })).toBeNull();
    await user.click(submit);
    expect(await findToast("Give a reason first.")).toBeTruthy();
    expect(backend.callsTo(COMMANDS)).toHaveLength(0);

    await user.type(screen.getByLabelText(/^Reason/), " First operator ");
    await user.click(submit);
    expect(await findToast("Operator identity imported.")).toBeTruthy();
    const sent = body(backend.callsTo(COMMANDS)[0]);
    expect(sent).toMatchObject({
      action: "bootstrap",
      reason: "First operator",
      arguments: {},
    });
    expect(sent.request_id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("links a second login issuer to a named account on import", async () => {
    const { user, backend } = open({ [USERS]: { status: 403 } });
    await user.type(
      await screen.findByLabelText(/^Existing platform user ID/),
      platformIds.operator,
    );
    await user.type(screen.getByLabelText(/^Reason/), "Second issuer");
    await user.click(
      screen.getByRole("button", { name: "Import my operator identity" }),
    );
    await waitFor(() => expect(backend.callsTo(COMMANDS)).toHaveLength(1));
    expect(body(backend.callsTo(COMMANDS)[0]).arguments).toEqual({
      user_id: platformIds.operator,
    });
  });

  it("prepares a seven-day invitation from a name and a reason", async () => {
    const { user, backend } = open();
    const submit = await screen.findByRole("button", {
      name: "Prepare seven-day invitation",
    });
    await user.click(submit);
    expect(await findToast("Name the friend first.")).toBeTruthy();
    expect(backend.callsTo(COMMANDS)).toHaveLength(0);

    await user.type(screen.getByLabelText(/^Friend's name/), "  Sorrel ");
    await user.type(screen.getByLabelText(/^Reason/), "New player");
    await user.click(submit);
    expect(
      await findToast(
        "Invitation prepared. Manage it to show the signup link.",
      ),
    ).toBeTruthy();
    expect(body(backend.callsTo(COMMANDS)[0])).toMatchObject({
      action: "issue",
      reason: "New player",
      arguments: { recipient_label: "Sorrel", expires_in_days: 7 },
    });
    expect(backend.callsTo(USERS).length).toBeGreaterThan(1);
    expect(
      (screen.getByLabelText(/^Friend's name/) as HTMLInputElement).value,
    ).toBe("");
  });

  it("shows a signup link once, to copy", async () => {
    const { user, backend } = open();
    const dialog = await manage(user, "Manage invitation for Juniper");
    await user.click(
      within(dialog).getByRole("button", { name: "Show signup link once" }),
    );
    const link = (await screen.findByLabelText(
      "Private signup link",
    )) as HTMLInputElement;
    expect(link.value).toBe(`${location.origin}/register#${platformToken}`);
    expect(link.readOnly).toBe(true);
    const [call] = backend.callsTo(DELIVER);
    expect(call?.params).toEqual({ invitation_id: platformIds.unshown });
    expect(body(call)).toMatchObject({
      reason: "Agreed at the table",
      reissue: false,
    });
    expect(body(call).request_id).toMatch(/^[0-9a-f-]{36}$/);

    await user.click(screen.getByRole("button", { name: "Copy link" }));
    expect(await navigator.clipboard.readText()).toBe(link.value);
    await user.click(screen.getByRole("button", { name: "Hide link" }));
    await waitFor(() =>
      expect(screen.queryByLabelText("Private signup link")).toBeNull(),
    );
  });

  it("confirms before a reissue invalidates the shared link", async () => {
    const { user, backend } = open();
    const dialog = await manage(user, "Manage invitation for Corin");
    await user.click(
      within(dialog).getByRole("button", { name: "Reissue link" }),
    );
    const alert = await screen.findByRole("alertdialog");
    expect(
      within(alert).getByText(
        "The link already shared with Corin stops working.",
      ),
    ).toBeTruthy();
    expect(backend.callsTo(DELIVER)).toHaveLength(0);
    await user.click(
      within(alert).getByRole("button", { name: "Reissue link" }),
    );
    expect(await screen.findByLabelText("Private signup link")).toBeTruthy();
    const [call] = backend.callsTo(DELIVER);
    expect(call?.params).toEqual({ invitation_id: platformIds.shown });
    expect(body(call).reissue).toBe(true);
  });

  it("revokes an invitation after confirmation", async () => {
    const { user, backend } = open();
    const dialog = await manage(user, "Manage invitation for Juniper");
    await user.click(
      within(dialog).getByRole("button", { name: "Revoke invitation" }),
    );
    const alert = await screen.findByRole("alertdialog");
    await user.click(within(alert).getByRole("button", { name: "Cancel" }));
    expect(backend.callsTo(COMMANDS)).toHaveLength(0);

    const again = await manage(user, "Manage invitation for Juniper");
    await user.click(
      within(again).getByRole("button", { name: "Revoke invitation" }),
    );
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Revoke invitation",
      }),
    );
    expect(await findToast("Invitation revoked.")).toBeTruthy();
    expect(body(backend.callsTo(COMMANDS)[0])).toMatchObject({
      action: "revoke_invitation",
      arguments: { invitation_id: platformIds.unshown },
    });
  });

  it("needs a reason before any change", async () => {
    const { user, backend } = open();
    await user.click(
      await screen.findByRole("button", { name: "Manage tansy" }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Grant grimoire.access" }),
    );
    expect(
      await within(dialog).findByText("Give a reason first."),
    ).toBeTruthy();
    expect(backend.callsTo(COMMANDS)).toHaveLength(0);
  });

  it("grants a permission and keeps the account open", async () => {
    const { user, backend } = open();
    const dialog = await manage(user, "Manage tansy");
    await user.click(
      within(dialog).getByRole("button", { name: "Grant grimoire.access" }),
    );
    expect(await findToast("Permission granted.")).toBeTruthy();
    expect(body(backend.callsTo(COMMANDS)[0])).toMatchObject({
      action: "grant",
      reason: "Agreed at the table",
      arguments: {
        user_id: platformIds.disabled,
        permission: "grimoire.access",
      },
    });
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("revokes a permission after confirmation", async () => {
    const { user, backend } = open();
    const dialog = await manage(user, "Manage wren");
    expect(
      within(dialog).getByRole("button", {
        name: "Grant grimoire.create_game",
      }),
    ).toBeTruthy();
    await user.click(
      within(dialog).getByRole("button", { name: "Revoke grimoire.access" }),
    );
    const alert = await screen.findByRole("alertdialog");
    expect(within(alert).getByText("wren loses grimoire.access.")).toBeTruthy();
    await user.click(
      within(alert).getByRole("button", { name: "Revoke permission" }),
    );
    expect(await findToast("Permission revoked.")).toBeTruthy();
    expect(body(backend.callsTo(COMMANDS)[0])).toMatchObject({
      action: "revoke_grant",
      arguments: { user_id: platformIds.friend, permission: "grimoire.access" },
    });
  });

  it("disables an account after confirmation and enables one directly", async () => {
    const { user, backend } = open();
    const dialog = await manage(user, "Manage wren");
    await user.click(
      within(dialog).getByRole("button", { name: "Disable platform access" }),
    );
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Disable platform access",
      }),
    );
    expect(await findToast("Platform access disabled.")).toBeTruthy();
    expect(body(backend.callsTo(COMMANDS)[0])).toMatchObject({
      action: "set_active",
      arguments: { user_id: platformIds.friend, active: false },
    });

    const disabled = await manage(user, "Manage tansy", "Back from a break");
    await user.click(
      within(disabled).getByRole("button", { name: "Enable platform account" }),
    );
    expect(await findToast("Platform account enabled.")).toBeTruthy();
    expect(body(backend.callsTo(COMMANDS)[1])).toMatchObject({
      action: "set_active",
      reason: "Back from a break",
      arguments: { user_id: platformIds.disabled, active: true },
    });
  });

  it("retries a failed change under the same request id and a new change under a new one", async () => {
    let attempts = 0;
    const { user, backend } = open({
      [COMMANDS]: () =>
        ++attempts === 1
          ? { status: 409, detail: "Platform state changed. Retry." }
          : { ...platformUsers[2], audit_reference: "audit" },
    });
    const dialog = await manage(user, "Manage tansy");
    const access = within(dialog).getByRole("button", {
      name: "Grant grimoire.access",
    });
    await user.click(access);
    expect(await findToast("Platform state changed. Retry.")).toBeTruthy();
    await user.click(access);
    await waitFor(() => expect(backend.callsTo(COMMANDS)).toHaveLength(2));
    await findToast("Permission granted.");
    await user.click(
      within(dialog).getByRole("button", {
        name: "Grant grimoire.create_game",
      }),
    );
    await waitFor(() => expect(backend.callsTo(COMMANDS)).toHaveLength(3));
    await user.click(access);
    await waitFor(() => expect(backend.callsTo(COMMANDS)).toHaveLength(4));
    const ids = backend.callsTo(COMMANDS).map((call) => body(call).request_id);
    expect(ids[1]).toBe(ids[0]);
    expect(new Set(ids).size).toBe(3);
  });

  it("opens the invitation a delivery reference names, even beyond the first page", async () => {
    const { backend } = open(
      {
        [INVITATION]: platformInvitation({
          id: platformIds.beyondFirstPage,
          recipient_label: "Bracken",
        }),
      },
      `/platform?invitation_id=${platformIds.beyondFirstPage}`,
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Bracken")).toBeTruthy();
    expect(
      within(dialog).getByRole("button", { name: "Show signup link once" }),
    ).toBeTruthy();
    expect(backend.callsTo(INVITATION)[0]?.query.get("invitation_id")).toBe(
      platformIds.beyondFirstPage,
    );
    expect(
      screen
        .getAllByRole("listitem")
        .some((row) => within(row).queryByText("Bracken")),
    ).toBe(true);
  });

  it("does not look up a listed invitation again", async () => {
    const { backend } = open(
      {},
      `/platform?invitation_id=${platformInvitations[0]!.id}`,
    );
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(backend.callsTo(INVITATION)).toHaveLength(0);
  });

  it("keeps the accounts when the named invitation does not exist", async () => {
    open(
      {
        [INVITATION]: { status: 404, detail: "Platform invitation not found." },
      },
      `/platform?invitation_id=${platformIds.beyondFirstPage}`,
    );
    expect(await screen.findByText("Invitation not found")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Users" })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows the refusal the backend gave and keeps the confirmation open", async () => {
    const { user } = open({
      [DELIVER]: {
        status: 410,
        detail: "Invitation expired, revoked or accepted.",
      },
    });
    const dialog = await manage(user, "Manage invitation for Juniper");
    await user.click(
      within(dialog).getByRole("button", { name: "Show signup link once" }),
    );
    expect(
      await findToast("Invitation expired, revoked or accepted."),
    ).toBeTruthy();
    expect(screen.queryByLabelText("Private signup link")).toBeNull();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});
