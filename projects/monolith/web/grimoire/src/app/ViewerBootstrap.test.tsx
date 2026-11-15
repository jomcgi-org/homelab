import { act, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { keys } from "../api/lobby";
import { platformKeys } from "../api/platform";
import projection from "../lib/fixtures/reveal-projections.json";
import { openApp } from "../test/app";
import { campaigns, ids, lobby } from "../test/fixtures";
import {
  platformIds,
  platformOther,
  platformUser,
} from "../test/fixtures/platform";
import { paths } from "./paths";

const SELF = "GET /api/auth/platform/self";
const ENTITY = "GET /campaigns/{campaign_id}/entities/{entity_id}";
const first = platformUser({ permissions: [] });
const second = platformUser({
  id: platformIds.operator,
  username: "bram",
  display_name: "Bram",
  permissions: [],
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function reloadSpy() {
  const reload = vi.fn();
  vi.stubGlobal("location", { origin: "https://friends.example.test", reload });
  return reload;
}

async function poll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000);
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function profile(lobbyStatus = 403) {
  let own: unknown = first;
  const app = openApp(
    { "GET /lobby": { status: lobbyStatus, detail: "Lobby unavailable." } },
    paths.profile,
    { ...platformOther, [SELF]: () => own },
  );
  return {
    ...app,
    setAccount: (next: unknown) => {
      own = next;
    },
  };
}

describe("viewer identity bootstrap", () => {
  it("waits for the platform identity before reading its private directory", async () => {
    let own: unknown = {
      status: 503,
      detail: "Identity temporarily unavailable.",
    };
    const app = openApp({ "GET /lobby": { status: 403 } }, paths.platform, {
      ...platformOther,
      [SELF]: () => own,
    });
    await screen.findByText("Identity temporarily unavailable.");
    expect(screen.queryByRole("button", { name: "Feedback" })).toBeNull();
    expect(
      app.backend.callsTo("GET /api/auth/platform/management/users"),
    ).toHaveLength(0);
    expect(screen.queryByRole("region", { name: "Users" })).toBeNull();

    own = first;
    await act(async () => {
      await app.client.invalidateQueries({ queryKey: platformKeys.self });
    });
    await screen.findByRole("region", { name: "Users" });
  });

  it("waits for a profile identity before offering a private feedback draft", async () => {
    let own: unknown = {
      status: 503,
      detail: "Identity temporarily unavailable.",
    };
    const app = openApp({ "GET /lobby": { status: 403 } }, paths.profile, {
      [SELF]: () => own,
    });
    await screen.findByText("Identity temporarily unavailable.");
    expect(screen.queryByRole("button", { name: "Feedback" })).toBeNull();

    own = second;
    await act(async () => {
      await app.client.invalidateQueries({ queryKey: platformKeys.self });
    });
    await screen.findByText("Welcome, Bram.");
    expect(screen.getByRole("button", { name: "Feedback" })).toBeTruthy();
  });

  it("keeps explicit platform bootstrap available without either application account", async () => {
    const reload = reloadSpy();
    openApp({ "GET /lobby": { status: 403 } }, paths.platform, {
      ...platformOther,
      [SELF]: { status: 403 },
      "GET /api/auth/platform/management/users": { status: 403 },
    });
    await screen.findByText(/An active platform operator account is required/);
    expect(reload).not.toHaveBeenCalled();
  });

  it("waits for identity before reading a private knowledge entry", async () => {
    const reload = reloadSpy();
    const privateEntity = {
      ...projection.cases[0]!.expected,
      name: "Previous viewer's private canary",
    };
    const app = openApp(
      {
        "GET /lobby": {
          status: 503,
          detail: "Identity temporarily unavailable.",
        },
        [ENTITY]: privateEntity,
      },
      paths.entity(ids.owned, privateEntity.id),
    );
    await screen.findByText("Identity temporarily unavailable.");
    expect(app.backend.callsTo(ENTITY)).toHaveLength(0);
    expect(
      screen.queryByRole("heading", { name: privateEntity.name }),
    ).toBeNull();

    app.backend.set({
      "GET /lobby": lobby({
        user: {
          id: "88888888-8888-4888-8888-888888888888",
          email: "bram@example.test",
          display_name: "Bram",
        },
        campaigns: [
          {
            ...campaigns.owned,
            role: "player",
            is_owner: false,
            player_character_id: ids.spare,
          },
        ],
      }),
      [ENTITY]: { status: 404 },
    });
    await act(async () => {
      await app.client.invalidateQueries({ queryKey: keys.lobby });
    });
    await screen.findByText("This knowledge entry is not available to you.");
    expect(
      screen.queryByRole("heading", { name: privateEntity.name }),
    ).toBeNull();
    expect(reload).not.toHaveBeenCalled();
  });

  it.each([403, 503])(
    "fences an account switch while the lobby keeps returning %s",
    async (status) => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      const reload = reloadSpy();
      const app = profile(status);
      await screen.findByText("Welcome, Wren Alder.");
      app.client.setQueryData(
        ["feedback", "draft"],
        "First account's private draft",
      );
      app.setAccount(second);

      await poll();

      expect(screen.queryByText("Welcome, Wren Alder.")).toBeNull();
      expect(app.client.getQueryCache().getAll()).toHaveLength(0);
      expect(reload).toHaveBeenCalledTimes(1);
    },
  );

  it.each([403, 404])(
    "removes a cached profile when its own identity read is denied with %s",
    async (status) => {
      const reload = reloadSpy();
      const app = profile();
      await screen.findByText("Welcome, Wren Alder.");
      app.setAccount({ status, detail: "Profile unavailable." });

      await act(async () => {
        await app.client.invalidateQueries({ queryKey: platformKeys.self });
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

      expect(screen.queryByText("Welcome, Wren Alder.")).toBeNull();
      expect(app.client.getQueryCache().getAll()).toHaveLength(0);
      expect(reload).toHaveBeenCalledTimes(1);
    },
  );

  it("keeps legitimate profile access when Grimoire is denied and the account is unchanged", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const reload = reloadSpy();
    const app = profile();
    await screen.findByText("Welcome, Wren Alder.");
    app.client.setQueryData(["feedback", "draft"], "Still writing");

    await poll();

    expect(screen.getByText("Welcome, Wren Alder.")).toBeTruthy();
    expect(
      screen.getByText(/Ask your administrator to enable Grimoire/),
    ).toBeTruthy();
    expect(app.client.getQueryData(["feedback", "draft"])).toBe(
      "Still writing",
    );
    expect(reload).not.toHaveBeenCalled();
  });

  it("keeps the same profile during a temporary self-read failure", async () => {
    const reload = reloadSpy();
    const app = profile();
    await screen.findByText("Welcome, Wren Alder.");
    app.setAccount({
      status: 503,
      detail: "Identity temporarily unavailable.",
    });

    await act(async () => {
      await app.client.invalidateQueries({ queryKey: platformKeys.self });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText("Welcome, Wren Alder.")).toBeTruthy();
    expect(reload).not.toHaveBeenCalled();
  });
});
