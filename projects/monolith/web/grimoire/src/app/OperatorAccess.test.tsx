import { act, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { platformKeys } from "../api/platform";
import { findToast, openApp } from "../test/app";
import {
  platformIds,
  platformOther,
  platformPermissions,
  platformUser,
  platformUsers,
} from "../test/fixtures/platform";
import { paths } from "./paths";

const PERMISSIONS = "GET /api/auth/platform/management/permissions";
const USERS = "GET /api/auth/platform/management/users";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function open(initialStatus?: number, path = paths.platform) {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const reload = vi.fn();
  vi.stubGlobal("location", { origin: "https://friends.example.test", reload });
  let permissionStatus: number | undefined;
  let directoryStatus = initialStatus;
  const app = openApp({ "GET /lobby": { status: 403 } }, path, {
    ...platformOther,
    "GET /api/auth/platform/self": platformUser({ permissions: [] }),
    [PERMISSIONS]: () =>
      permissionStatus
        ? { status: permissionStatus, detail: "Operator access unavailable." }
        : platformPermissions,
    [USERS]: () =>
      directoryStatus
        ? { status: directoryStatus, detail: "Directory unavailable." }
        : { items: platformUsers, next: null },
  });
  return {
    ...app,
    reload,
    permissions: (status: number) => {
      permissionStatus = status;
    },
    directory: (status: number) => {
      directoryStatus = status;
    },
  };
}

async function poll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000);
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function manage(app: ReturnType<typeof open>) {
  await screen.findByRole("region", { name: "Users" });
  await app.user.click(screen.getByRole("button", { name: "Manage wren" }));
  const dialog = await screen.findByRole("dialog");
  await app.user.type(
    within(dialog).getByLabelText(/^Reason/),
    "Private operator draft",
  );
  app.client.setQueryData(["feedback", "draft"], "Private operator feedback");
  return dialog;
}

function cleared(app: ReturnType<typeof open>) {
  // Disabled identity observers may recreate empty cache entries after clear.
  // No previous payload or draft may survive in any of them.
  for (const query of app.client.getQueryCache().getAll())
    expect(query.state.data).toBeUndefined();
  expect(app.client.getQueryData(["feedback", "draft"])).toBeUndefined();
  expect(app.reload).toHaveBeenCalledTimes(1);
}

async function staysFenced(app: ReturnType<typeof open>) {
  const operations = [
    USERS,
    PERMISSIONS,
    "GET /api/auth/platform/management/invitations",
    "GET /api/auth/platform/management/invitation",
    "GET /api/auth/platform/self",
    "GET /lobby",
  ];
  const counts = operations.map(
    (operation) => app.backend.callsTo(operation).length,
  );
  await poll();
  await poll();
  await act(async () => {
    await app.client.invalidateQueries();
  });
  expect(
    operations.map((operation) => app.backend.callsTo(operation).length),
  ).toEqual(counts);
  cleared(app);
}

describe("operator access boundary", () => {
  it.each([403, 404])(
    "clears the directory and open dialogs after access is refused with %s",
    async (status) => {
      const app = open();
      await manage(app);
      app.permissions(status);

      await poll();

      expect(screen.queryByRole("region", { name: "Users" })).toBeNull();
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(screen.queryByDisplayValue("Private operator draft")).toBeNull();
      cleared(app);
      await staysFenced(app);
    },
  );

  it("keeps checking established operator access after leaving the directory", async () => {
    const app = open();
    await screen.findByRole("region", { name: "Users" });
    await app.user.click(
      screen.getByRole("link", { name: "Back to Grimoire" }),
    );
    await screen.findByRole("heading", { name: "Could not load your account" });
    expect(app.client.getQueryData(platformKeys.directory(null))).toBeTruthy();
    app.permissions(403);

    await poll();

    cleared(app);
    await staysFenced(app);
  });

  it("fences a confirmed directory denial without waiting for the next poll", async () => {
    const app = open();
    await manage(app);
    app.directory(403);

    await act(async () => {
      await app.client.invalidateQueries({
        queryKey: platformKeys.directory(null),
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.queryByRole("region", { name: "Users" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    cleared(app);
    await staysFenced(app);
  });

  it("removes a selected invitation's private signup link and queued toast", async () => {
    const app = open(
      undefined,
      `${paths.platform}?invitation_id=${platformIds.unshown}`,
    );
    const dialog = await screen.findByRole("dialog");
    const show = within(dialog).getByRole("button", {
      name: "Show signup link once",
    });
    await app.user.click(show);
    await findToast("Give a reason first.");
    await app.user.type(
      within(dialog).getByLabelText(/^Reason/),
      "Private invitation reason",
    );
    await app.user.click(show);
    await screen.findByLabelText("Private signup link");
    app.directory(403);

    await act(async () => {
      await app.client.invalidateQueries({
        queryKey: platformKeys.directory(platformIds.unshown),
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.queryByLabelText("Private signup link")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryAllByText("Give a reason first.")).toHaveLength(0);
    cleared(app);
    await staysFenced(app);
  });

  it("retains the established directory and drafts during temporary failures", async () => {
    const app = open();
    const dialog = await manage(app);
    app.permissions(503);
    app.directory(503);

    await poll();
    await act(async () => {
      await app.client.invalidateQueries({
        queryKey: platformKeys.directory(null),
      });
    });

    expect(app.client.getQueryData(platformKeys.directory(null))).toBeTruthy();
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(screen.getByDisplayValue("Private operator draft")).toBeTruthy();
    expect(app.client.getQueryData(["feedback", "draft"])).toBe(
      "Private operator feedback",
    );
    expect(app.reload).not.toHaveBeenCalled();
  });

  it("keeps an initial refusal on Bootstrap without a reload loop", async () => {
    const app = open(403);
    await screen.findByRole("button", { name: "Import my operator identity" });
    const reads = app.backend.callsTo(PERMISSIONS).length;

    await poll();

    expect(
      screen.getByRole("button", { name: "Import my operator identity" }),
    ).toBeTruthy();
    expect(app.backend.callsTo(PERMISSIONS)).toHaveLength(reads);
    expect(app.reload).not.toHaveBeenCalled();
  });
});
