import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { openApp } from "../../test/app";
import { platformIds, platformUser } from "../../test/fixtures/platform";

const SELF = "GET /api/auth/platform/self";
const open = (reply: unknown) =>
  openApp({}, "/platform/profile", { [SELF]: reply });

describe("Profile", () => {
  it("welcomes an account with Grimoire access and links to the lobby", async () => {
    open(platformUser());
    expect(await screen.findByText("Welcome, Wren Alder.")).toBeTruthy();
    expect(
      screen.getByText("Your account is registered as wren."),
    ).toBeTruthy();
    const link = screen.getByRole("link", { name: "Open Grimoire" });
    expect(link.getAttribute("href")).toBe("/");
    expect(screen.queryByText(/Your platform user ID/)).toBeNull();
  });

  it("tells an account without access who to ask, with its id", async () => {
    open(platformUser({ permissions: ["grimoire.create_game"] }));
    expect(
      await screen.findByText(/Ask your administrator to enable Grimoire/),
    ).toBeTruthy();
    expect(
      screen.getByText(`Your platform user ID: ${platformIds.friend}`),
    ).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Open Grimoire" })).toBeNull();
  });

  it("is unavailable while management is switched off", async () => {
    open({ status: 404 });
    expect(
      await screen.findByText("Account management is unavailable."),
    ).toBeTruthy();
  });

  it("shows why an account was refused", async () => {
    open({ status: 403, detail: "Active platform account required." });
    expect(
      await screen.findByText("Active platform account required."),
    ).toBeTruthy();
  });
});
