import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { miraSheet } from "../test/fixtures/sheets";
import {
  clearAccountSheetRecovery, isSheetSignOut, keepSheetRecovery, purgeSheetRecovery,
  restoreSheetRecovery, sheetRecoveryEpoch, sheetRecoveryExpires,
} from "./sheet-recovery";

const scope = { accountId: "a", campaignId: "table", characterId: "mira" };
const edited = Date.parse("2026-01-31T12:00:00Z");
const changed = { ...miraSheet, level: 4 };
let epoch: string | null;
const recoveryKeys = () => Object.keys(localStorage).filter((name) => name.startsWith("grimoire-sheet-recovery:"));
const keep = () => keepSheetRecovery(scope, epoch, edited, miraSheet, changed, {}, []);
beforeEach(() => { epoch = sheetRecoveryEpoch(scope.accountId); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("hidden sheet recovery", () => {
  it("keeps only touched values and merge bases, with no private snapshot or save authority", () => {
    keep();
    const record = localStorage.getItem(recoveryKeys()[0]!)!;
    expect(JSON.parse(record)).toEqual({
      format: 1, writer: "default", editedAt: edited,
      changes: { level: { base: 3, value: 4 } }, numericText: {}, conflicts: [],
    });
    expect(record).not.toContain("Wizard");
    expect(record).not.toContain("Elf");
    expect(restoreSheetRecovery(scope, miraSheet, edited + 1)?.sheet.level).toBe(4);
    for (const other of [
      { accountId: "b" }, { campaignId: "other" }, { characterId: "other" },
    ]) expect(restoreSheetRecovery({ ...scope, ...other }, miraSheet, edited + 1)).toBeNull();
  });

  it("merges only touched fields against fresh data and requires a choice on conflicts", () => {
    keep();
    const fresh = { ...miraSheet, level: 5, ancestry: "Human" };
    const recovered = restoreSheetRecovery(scope, fresh, edited + 1)!;
    expect(recovered.sheet).toEqual({ ...fresh, level: 4 });
    expect(recovered.conflicts).toEqual(["level"]);
    expect(recovered.editedAt).toBe(edited);
    expect(restoreSheetRecovery(scope, changed, edited + 1)?.conflicts).toEqual([]);
  });

  it("expires after a calendar month, including short months, without extending on reads", () => {
    keep();
    const expires = Date.parse("2026-02-28T12:00:00Z");
    expect(sheetRecoveryExpires(edited)).toBe(expires);
    expect(sheetRecoveryExpires(Date.parse("2024-01-31T12:00:00Z"))).toBe(Date.parse("2024-02-29T12:00:00Z"));
    expect(restoreSheetRecovery(scope, miraSheet, expires - 1)).not.toBeNull();
    purgeSheetRecovery(expires);
    expect(recoveryKeys()).toHaveLength(0);
    expect(restoreSheetRecovery(scope, miraSheet, expires)).toBeNull();
  });

  it("retains raw unfinished number input and clears a saved or discarded buffer", () => {
    keepSheetRecovery(scope, epoch, edited, miraSheet, miraSheet, { level: "" }, []);
    expect(restoreSheetRecovery(scope, miraSheet, edited + 1)?.numericText).toEqual({ level: "" });
    keepSheetRecovery(scope, epoch, edited, miraSheet, miraSheet, {}, []);
    expect(recoveryKeys()).toHaveLength(0);
  });

  it("does not let an untouched or older tab erase newer unsaved edits", () => {
    const now = Date.now();
    keepSheetRecovery(scope, epoch, now, miraSheet, changed, {}, [], "newer-tab");
    keepSheetRecovery(scope, epoch, now - 1, miraSheet, miraSheet, {}, [], "clean-tab");
    keepSheetRecovery(scope, epoch, now - 1, miraSheet, { ...changed, level: 5 }, {}, [], "older-tab");
    expect(restoreSheetRecovery(scope, miraSheet)?.sheet.level).toBe(4);
  });

  it("clears only the signing-out account and fences late writes from its old editors", () => {
    keep();
    const other = { ...scope, accountId: "b" };
    keepSheetRecovery(other, sheetRecoveryEpoch("b"), edited, miraSheet, changed, {}, []);
    clearAccountSheetRecovery("a");
    expect(restoreSheetRecovery(scope, miraSheet, edited + 1)).toBeNull();
    expect(keep()).toBe(false);
    expect(sheetRecoveryEpoch("a")).not.toBe(epoch);
    expect(keep()).toBe(false);
    expect(restoreSheetRecovery(other, miraSheet, edited + 1)?.sheet.level).toBe(4);
    clearAccountSheetRecovery("b");
    expect(restoreSheetRecovery(other, miraSheet, edited + 1)).toBeNull();
  });

  it("purges malformed records and tolerates unavailable browser storage", () => {
    localStorage.setItem("grimoire-sheet-recovery:broken", "{");
    localStorage.setItem("unrelated", "keep");
    purgeSheetRecovery(edited);
    expect(recoveryKeys()).toHaveLength(0);
    expect(localStorage.getItem("unrelated")).toBe("keep");
    vi.stubGlobal("localStorage", undefined);
    expect(keep()).toBe(false);
    expect(restoreSheetRecovery(scope, miraSheet, edited + 1)).toBeNull();
    expect(() => clearAccountSheetRecovery("a")).not.toThrow();
  });

  it("clears every owned record at full quota and fences old tabs without writing a marker", () => {
    const other = { ...scope, accountId: "b" };
    keep();
    keepSheetRecovery({ ...scope, characterId: "second" }, epoch, edited, miraSheet, changed, {}, []);
    keepSheetRecovery(other, sheetRecoveryEpoch("b"), edited, miraSheet, changed, {}, []);
    localStorage.setItem("unrelated", "keep");
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Full", "QuotaExceededError");
    });
    clearAccountSheetRecovery("a");
    expect(write).not.toHaveBeenCalled();
    expect(recoveryKeys()).toHaveLength(1);
    expect(restoreSheetRecovery(scope, miraSheet, edited + 1)).toBeNull();
    expect(restoreSheetRecovery(other, miraSheet, edited + 1)?.sheet.level).toBe(4);
    expect(localStorage.getItem("unrelated")).toBe("keep");
    // Simulate an old tab that has not received its storage event yet.
    expect(keep()).toBe(false);
    expect(isSheetSignOut("grimoire-sheet-sign-out:a", "a", null)).toBe(true);
    expect(isSheetSignOut("grimoire-sheet-sign-out:b", "a", null)).toBe(false);
    expect(isSheetSignOut("grimoire-sheet-sign-out:a", "a", "new-epoch")).toBe(false);
  });

  it("does not persist edits when the initial cross-tab fence cannot be allocated", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Full", "QuotaExceededError");
    });
    const other = { ...scope, accountId: "new-account" };
    const unavailable = sheetRecoveryEpoch(other.accountId);
    expect(unavailable).toBeNull();
    expect(keepSheetRecovery(other, unavailable, edited, miraSheet, changed, {}, [])).toBe(false);
    expect(recoveryKeys()).toHaveLength(0);
  });
});
