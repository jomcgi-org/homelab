import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Routes } from "../../test/backend";
import { findToast, openApp } from "../../test/app";
import { lobby } from "../../test/fixtures";
import { feedbackIds, feedbackItems } from "../../test/fixtures/feedback";
import { world } from "../../test/world";
import { paths } from "../../app/paths";

/** An inbox that forgets an item once it is filed or dismissed. */
function inbox(): Routes {
  let items = [...feedbackItems];
  const resolve = (id: string) => {
    items = items.filter((item) => item.id !== id);
  };
  return {
    ...world,
    "GET /feedback": () => items,
    "POST /feedback/{feedback_id}/file": (call) => {
      resolve(call.params.feedback_id!);
      return { issue_number: 4321, issue_url: "https://github.example.test/x" };
    },
    "POST /feedback/{feedback_id}/dismiss": (call) => {
      resolve(call.params.feedback_id!);
      return { status: 204 };
    },
  };
}

const rowan = { name: "Feedback from rowan@example.test" };
const wren = { name: "Feedback from wren@example.test" };

describe("Feedback inbox", () => {
  it("lists what was said, by whom, where and when", async () => {
    const { backend } = openApp(inbox(), paths.feedback);
    const item = await screen.findByRole("article", rowan);
    expect(within(item).getByText(/which one counted/)).toBeTruthy();
    expect(within(item).getByText(/Salt Marsh Nights/)).toBeTruthy();
    expect(within(item).getByText(/\/session/)).toBeTruthy();
    expect(within(item).getByText(/2026/)).toBeTruthy();
    expect(screen.getByRole("article", wren)).toBeTruthy();
    expect(backend.callsTo("GET /feedback")[0]?.query.get("status")).toBe(
      "new",
    );
  });

  it("files the edited title and body, then removes the item", async () => {
    const { user, backend } = openApp(inbox(), paths.feedback);
    const item = await screen.findByRole("article", wren);
    await user.click(within(item).getByRole("button", { name: "File issue" }));
    const dialog = await screen.findByRole("dialog", { name: "File issue" });
    expect(
      within(dialog).getByText(/published on the public repository/),
    ).toBeTruthy();
    const submit = within(dialog).getByRole("button", { name: "File issue" });
    const body = within(dialog).getByRole<HTMLTextAreaElement>("textbox", {
      name: /^Body/,
    });
    // The sender's words are shown for reference, never as the default body.
    expect(
      within(dialog).getByText("The spell list is hard to scan on a phone."),
    ).toBeTruthy();
    expect(body.value).toBe("");

    // The title is required: nothing is sent without one.
    await user.click(submit);
    expect(backend.callsTo("POST /feedback/{feedback_id}/file")).toEqual([]);

    await user.type(
      within(dialog).getByRole("textbox", { name: /^Title/ }),
      " Spell list on small screens ",
    );
    await user.type(body, "Group spells by level at 390px.");
    await user.click(submit);

    expect(await findToast("Filed as issue #4321.")).toBeTruthy();
    const [call] = backend.callsTo("POST /feedback/{feedback_id}/file");
    expect(call?.params).toEqual({ feedback_id: feedbackIds.sheets });
    expect(call?.body).toEqual({
      title: "Spell list on small screens",
      body: "Group spells by level at 390px.",
    });
    await waitFor(() => expect(screen.queryByRole("article", wren)).toBeNull());
    expect(screen.queryByRole("dialog", { name: "File issue" })).toBeNull();
    expect(screen.getByRole("article", rowan)).toBeTruthy();
  });

  it("keeps the item and the dialog when GitHub refuses", async () => {
    const refusal =
      "GitHub did not confirm the issue. Check the repository before filing again.";
    const { user } = openApp(
      {
        ...inbox(),
        "POST /feedback/{feedback_id}/file": { status: 502, detail: refusal },
      },
      paths.feedback,
    );
    const item = await screen.findByRole("article", wren);
    await user.click(within(item).getByRole("button", { name: "File issue" }));
    const dialog = await screen.findByRole("dialog", { name: "File issue" });
    await user.type(
      within(dialog).getByRole("textbox", { name: /^Title/ }),
      "Spell list",
    );
    await user.type(
      within(dialog).getByRole("textbox", { name: /^Body/ }),
      "Group spells by level.",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "File issue" }),
    );
    expect(await findToast(refusal)).toBeTruthy();
    expect(screen.getByRole("dialog", { name: "File issue" })).toBeTruthy();
    expect(screen.getByRole("article", wren)).toBeTruthy();
  });

  it("dismisses an item without asking", async () => {
    const { user, backend } = openApp(inbox(), paths.feedback);
    const item = await screen.findByRole("article", rowan);
    await user.click(within(item).getByRole("button", { name: "Dismiss" }));
    expect(await findToast("Dismissed.")).toBeTruthy();
    expect(
      backend.callsTo("POST /feedback/{feedback_id}/dismiss")[0]?.params,
    ).toEqual({ feedback_id: feedbackIds.dice });
    await waitFor(() =>
      expect(screen.queryByRole("article", rowan)).toBeNull(),
    );
    expect(screen.getByRole("article", wren)).toBeTruthy();
  });

  it("says so when nothing is waiting", async () => {
    openApp({ ...world, "GET /feedback": [] }, paths.feedback);
    expect(await screen.findByText("No new feedback.")).toBeTruthy();
  });

  it("refuses anyone who is not an operator", async () => {
    openApp(
      {
        ...world,
        "GET /feedback": { status: 403, detail: "operator access required" },
      },
      paths.feedback,
    );
    expect(
      await screen.findByText("The feedback inbox is for operators."),
    ).toBeTruthy();
    expect(screen.queryByRole("article")).toBeNull();
  });
});

describe("Lobby link to the inbox", () => {
  it("shows operators the inbox with its count", async () => {
    openApp({
      ...world,
      "GET /lobby": lobby({
        can_administer_accounts: true,
        new_feedback_count: 3,
      }),
    });
    const link = await screen.findByRole("link", {
      name: "Feedback inbox (3)",
    });
    expect(link.getAttribute("href")).toBe("/feedback");
  });

  it("drops the count at zero", async () => {
    openApp({
      ...world,
      "GET /lobby": lobby({ can_administer_accounts: true }),
    });
    expect(
      await screen.findByRole("link", { name: "Feedback inbox" }),
    ).toBeTruthy();
  });

  it("hides the inbox from everyone else", async () => {
    openApp({ ...world, "GET /lobby": lobby({ new_feedback_count: 3 }) });
    await screen.findByRole("heading", { name: "The Sunless Citadel" });
    expect(screen.queryByRole("link", { name: /Feedback inbox/ })).toBeNull();
  });
});
