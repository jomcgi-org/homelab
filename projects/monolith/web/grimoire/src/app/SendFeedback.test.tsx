import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findToast, openApp } from "../test/app";
import { ids } from "../test/fixtures";
import { roadmapItems } from "../test/fixtures/feedback";
import { world } from "../test/world";
import { paths } from "./paths";
import { signIn } from "./signIn";

afterEach(() => signIn.reset());

async function write(user: ReturnType<typeof openApp>["user"], text: string) {
  await user.click(await screen.findByRole("button", { name: "Feedback" }));
  const dialog = await screen.findByRole("dialog", { name: "Feedback" });
  await user.type(
    within(dialog).getByRole("textbox", { name: "What should change?" }),
    text,
  );
  return dialog;
}

describe("Sending feedback", () => {
  it("sends the page and the campaign from a campaign screen", async () => {
    const { user, backend } = openApp(world, paths.notes(ids.owned));
    // The lazy screen replaces the loading page, and its header with it.
    await screen.findByRole("heading", { name: "The Sunless Citadel notes" });
    const dialog = await write(user, "  Notes need a search box.  ");
    await user.click(within(dialog).getByRole("button", { name: "Send" }));
    expect(await findToast("Thanks. Sent.")).toBeTruthy();
    expect(backend.callsTo("POST /feedback").map((call) => call.body)).toEqual([
      {
        text: "Notes need a search box.",
        page: `/campaigns/${ids.owned}/notes`,
        campaign_id: ids.owned,
      },
    ]);
    expect(screen.queryByRole("dialog", { name: "Feedback" })).toBeNull();
  });

  it("sends no campaign from the lobby", async () => {
    const { user, backend } = openApp(world);
    const dialog = await write(user, "More colour, please.");
    await user.click(within(dialog).getByRole("button", { name: "Send" }));
    expect(await findToast("Thanks. Sent.")).toBeTruthy();
    expect(backend.callsTo("POST /feedback")[0]?.body).toEqual({
      text: "More colour, please.",
      page: "/",
    });
  });

  it("says who will see the note and links to what is already suggested", async () => {
    const { user } = openApp(world);
    const dialog = await write(user, "Half a thought");
    expect(
      within(dialog).getByText(/Everyone signed in can see this\./),
    ).toBeTruthy();
    const link = within(dialog).getByRole("link", {
      name: "See what's already suggested",
    });
    expect(link.getAttribute("href")).toBe(paths.roadmap);
    await user.click(link);
    expect(
      await screen.findByRole("heading", { name: "Roadmap" }),
    ).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Feedback" })).toBeNull();
  });

  it("shows a sent note on the roadmap without a reload", async () => {
    const sent = {
      ...roadmapItems[0]!,
      id: "f9999999-9999-4999-8999-999999999999",
      text: "Dark mode for handouts.",
      votes: 0,
      mine: true,
    };
    let items = roadmapItems;
    const { user } = openApp(
      {
        ...world,
        "GET /feedback/roadmap": () => items,
        "POST /feedback": () => {
          items = [sent, ...items];
          return { id: sent.id };
        },
      },
      paths.roadmap,
    );
    await screen.findByRole("tab", { name: "New (2)" });
    const dialog = await write(user, sent.text);
    await user.click(within(dialog).getByRole("button", { name: "Send" }));
    expect(await findToast("Thanks. Sent.")).toBeTruthy();
    expect(await screen.findByRole("tab", { name: "New (3)" })).toBeTruthy();
    expect(screen.getByText(sent.text)).toBeTruthy();
  });

  it("does not send an empty note", async () => {
    const { user, backend } = openApp(world);
    const dialog = await write(user, "   ");
    await user.click(within(dialog).getByRole("button", { name: "Send" }));
    expect(backend.callsTo("POST /feedback")).toEqual([]);
  });

  it("shows the server's message at the daily limit and keeps the text", async () => {
    const limit = "You have sent 10 notes in the last day. Try again tomorrow.";
    const { user } = openApp({
      ...world,
      "POST /feedback": { status: 429, detail: limit },
    });
    const dialog = await write(user, "One more thing.");
    await user.click(within(dialog).getByRole("button", { name: "Send" }));
    expect(await findToast(limit)).toBeTruthy();
    expect(
      within(dialog).getByRole<HTMLTextAreaElement>("textbox", {
        name: "What should change?",
      }).value,
    ).toBe("One more thing.");
  });

  it("keeps a draft when the dialog is closed without sending", async () => {
    const { user } = openApp(world);
    const dialog = await write(user, "Half a thought");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Feedback" })).toBeNull();
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: "Feedback" }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Feedback" }));
    const reopened = await screen.findByRole("dialog", { name: "Feedback" });
    expect(
      within(reopened).getByRole<HTMLTextAreaElement>("textbox", {
        name: "What should change?",
      }).value,
    ).toBe("Half a thought");
  });

  it("is hidden once the sign-in has lapsed", async () => {
    openApp(world);
    await screen.findByRole("heading", { name: "The Sunless Citadel" });
    expect(screen.getByRole("button", { name: "Feedback" })).toBeTruthy();
    // Two signals more than two seconds apart confirm an expiry.
    const now = Date.now();
    act(() => {
      signIn.signal();
      vi.spyOn(Date, "now").mockReturnValue(now + 3_000);
      signIn.signal();
    });
    vi.restoreAllMocks();
    expect(await screen.findByText("Signed out")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Feedback" })).toBeNull();
  });
});
