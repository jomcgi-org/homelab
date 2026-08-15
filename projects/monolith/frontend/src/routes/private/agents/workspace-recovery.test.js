import { describe, expect, test } from "vitest";
import {
  workspaceRecoveryMessage,
  workspaceRecoveryTitle,
} from "./workspace-recovery.js";

describe("workspaceRecoveryMessage", () => {
  test("returns null without workspace recovery data", () => {
    expect(workspaceRecoveryMessage({ usage: {} })).toBeNull();
  });

  test("returns null when recovery was not degraded", () => {
    expect(
      workspaceRecoveryMessage({
        workspace_recovery: { created: true, restored: true, degraded: null },
      }),
    ).toBeNull();
  });

  test("maps a denied restore", () => {
    expect(
      workspaceRecoveryMessage({
        usage: {
          workspace_recovery: {
            created: true,
            restored: false,
            degraded: "restore_denied",
          },
        },
      }),
    ).toBe("workspaceRecoveryRestoreDenied");
  });

  test("maps a restore fallback", () => {
    expect(
      workspaceRecoveryMessage({
        workspace_recovery: {
          created: true,
          restored: false,
          degraded: "restore_fallback",
        },
      }),
    ).toBe("workspaceRecoveryRestoreFallback");
  });

  test("maps an unknown degraded value", () => {
    expect(
      workspaceRecoveryMessage({
        workspace_recovery: { degraded: "future_degraded_state" },
      }),
    ).toBe("workspaceRecoveryUnknown");
  });

  test("returns null when a turn has no usage", () => {
    expect(workspaceRecoveryMessage({ seq: 1 })).toBeNull();
  });

  test.each([null, undefined])("returns null for %s", (turn) => {
    expect(workspaceRecoveryMessage(turn)).toBeNull();
  });
});

describe("workspaceRecoveryTitle", () => {
  test("returns null without workspace recovery data", () => {
    expect(workspaceRecoveryTitle({ usage: {} })).toBeNull();
  });

  test("returns null when recovery was not degraded", () => {
    expect(
      workspaceRecoveryTitle({
        workspace_recovery: { created: true, restored: true, degraded: null },
      }),
    ).toBeNull();
  });

  test("provides title for denied restore", () => {
    expect(
      workspaceRecoveryTitle({
        usage: {
          workspace_recovery: {
            created: true,
            restored: false,
            degraded: "restore_denied",
          },
        },
      }),
    ).toBe("Workspace restore create was denied or timed out");
  });

  test("provides title for restore fallback", () => {
    expect(
      workspaceRecoveryTitle({
        workspace_recovery: {
          created: true,
          restored: false,
          degraded: "restore_fallback",
        },
      }),
    ).toBe("Workspace restore completed without restoring the session");
  });

  test("provides generic title for unknown degraded value", () => {
    expect(
      workspaceRecoveryTitle({
        workspace_recovery: { degraded: "future_degraded_state" },
      }),
    ).toBe("Workspace degradation cause unknown");
  });

  test("returns null when a turn has no usage", () => {
    expect(workspaceRecoveryTitle({ seq: 1 })).toBeNull();
  });
});
