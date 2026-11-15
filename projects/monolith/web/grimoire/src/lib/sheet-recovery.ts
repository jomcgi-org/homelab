/** The #8001 exception: hidden, account/character-scoped unsaved edits only. */
import type { CharacterSheetV1 } from "../api/schema";
import {
  fieldValue,
  mergeSheet,
  sheetFields,
  withField,
  withNumericText,
  type NumericText,
  type SheetField,
} from "../screens/sheets/merge";

export interface SheetScope {
  accountId: string;
  campaignId: string;
  characterId: string;
}

interface Change {
  base: string | number;
  value: string | number;
}

interface Buffer {
  format: 1;
  writer: string;
  editedAt: number;
  changes: Partial<Record<SheetField, Change>>;
  numericText: NumericText;
  conflicts: SheetField[];
}

const PREFIX = "grimoire-sheet-recovery:";
const SIGN_OUT = "grimoire-sheet-sign-out:";
export const sheetSignOutEvent = "grimoire-sheet-sign-out";
const key = (scope: SheetScope) =>
  PREFIX + JSON.stringify([scope.accountId, scope.campaignId, scope.characterId]);

function storage() {
  try {
    return globalThis.localStorage;
  } catch {
    return undefined;
  }
}

/** One calendar month in UTC, clamped to the last day of the next month. */
export function sheetRecoveryExpires(editedAt: number) {
  const date = new Date(editedAt);
  const day = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCMonth(date.getUTCMonth() + 1);
  const end = new Date(date);
  end.setUTCMonth(end.getUTCMonth() + 1, 0);
  date.setUTCDate(Math.min(day, end.getUTCDate()));
  return date.getTime();
}

function parse(raw: string | null, now: number): Buffer | null {
  try {
    const buffer = JSON.parse(raw ?? "null") as Buffer | null;
    if (
      !buffer ||
      buffer.format !== 1 ||
      typeof buffer.writer !== "string" ||
      !Number.isFinite(buffer.editedAt) ||
      buffer.editedAt > now ||
      !Number.isFinite(sheetRecoveryExpires(buffer.editedAt)) ||
      sheetRecoveryExpires(buffer.editedAt) <= now ||
      !buffer.changes ||
      typeof buffer.changes !== "object" ||
      Array.isArray(buffer.changes) ||
      !buffer.numericText ||
      typeof buffer.numericText !== "object" ||
      Array.isArray(buffer.numericText) ||
      !Array.isArray(buffer.conflicts)
    )
      return null;
    const fields = sheetFields.map(({ name }) => name);
    for (const [name, change] of Object.entries(buffer.changes)) {
      if (!fields.includes(name as SheetField) || !change) return null;
      const text = name === "ancestry" || name === "class_name";
      for (const value of [change.base, change.value]) {
        if (
          text
            ? typeof value !== "string" || value.length > 120
            : typeof value !== "number" || !Number.isFinite(value)
        ) return null;
      }
    }
    for (const [name, raw] of Object.entries(buffer.numericText)) {
      if (
        !fields.includes(name as SheetField) ||
        name === "ancestry" || name === "class_name" ||
        typeof raw !== "string" || raw.length > 120
      )
        return null;
    }
    if (buffer.conflicts.some((name) => !fields.includes(name))) return null;
    return buffer;
  } catch {
    return null;
  }
}

/** Purge expired/malformed records on app load, without exposing any contents. */
export function purgeSheetRecovery(now = Date.now()) {
  try {
    const store = storage();
    if (!store) return;
    for (const name of Object.keys(store)) {
      if (name.startsWith(PREFIX) && !parse(store.getItem(name), now))
        store.removeItem(name);
    }
  } catch {
    /* Storage can be unavailable or full. */
  }
}

/** Fences late effects and other open editors after explicit sign-out. */
export function sheetRecoveryEpoch(accountId: string) {
  try {
    return storage()?.getItem(SIGN_OUT + accountId) ?? "";
  } catch {
    return "";
  }
}

export function clearAccountSheetRecovery(accountId?: string) {
  try {
    const store = storage();
    if (store) {
      if (accountId) store.setItem(SIGN_OUT + accountId, crypto.randomUUID());
      for (const name of Object.keys(store)) {
        if (!name.startsWith(PREFIX)) continue;
        try {
          const owner = JSON.parse(name.slice(PREFIX.length))[0];
          if (!accountId || owner === accountId) {
            if (!accountId) store.setItem(SIGN_OUT + owner, crypto.randomUUID());
            store.removeItem(name);
          }
        } catch {
          store.removeItem(name);
        }
      }
    }
  } catch {
    /* Storage refusal must not prevent sign-out. */
  } finally {
    globalThis.dispatchEvent(
      new CustomEvent(sheetSignOutEvent, { detail: accountId }),
    );
  }
}

export function isSheetSignOut(key: string | null, accountId: string) {
  return key === SIGN_OUT + accountId;
}

export function dropSheetRecovery(scope: SheetScope) {
  try {
    storage()?.removeItem(key(scope));
  } catch {
    /* No durable copy is available. */
  }
}

/** Persist only changed fields and their merge bases, never a fetched sheet. */
export function keepSheetRecovery(
  scope: SheetScope,
  epoch: string,
  editedAt: number,
  saved: CharacterSheetV1,
  sheet: CharacterSheetV1,
  numericText: NumericText,
  conflicts: SheetField[],
  writer = "default",
  claim = false,
) {
  if (sheetRecoveryEpoch(scope.accountId) !== epoch) return false;
  const local = withNumericText(sheet, numericText);
  const changes: Buffer["changes"] = {};
  const typing: NumericText = {};
  for (const { name } of sheetFields) {
    const base = fieldValue(saved, name);
    const value = fieldValue(local, name);
    const raw =
      name === "ancestry" || name === "class_name"
        ? undefined
        : numericText[name];
    if (value !== base || (raw !== undefined && raw !== String(base))) {
      changes[name] = { base, value };
      if (raw !== undefined && name !== "ancestry" && name !== "class_name")
        typing[name] = raw.slice(0, 120);
    }
  }
  try {
    const store = storage();
    const dirty = Object.keys(changes).length > 0;
    if (!store) return !dirty;
    const previous = parse(store.getItem(key(scope)), Date.now());
    // A clean or older tab must not erase newer unsaved work in another tab.
    if (
      previous && previous.writer !== writer && !claim &&
      (!dirty || previous.editedAt >= editedAt)
    ) return true;
    if (!dirty) {
      store.removeItem(key(scope));
      return true;
    }
    store.setItem(key(scope), JSON.stringify({
      format: 1,
      writer,
      editedAt,
      changes,
      numericText: typing,
      conflicts: conflicts.filter((name) => name in changes),
    } satisfies Buffer));
    return true;
  } catch {
    return false;
  }
}

/** Call only after a fresh, identity-bound, authorized sheet/options read. */
export function restoreSheetRecovery(
  scope: SheetScope,
  current: CharacterSheetV1,
  now = Date.now(),
) {
  let buffer: Buffer | null;
  try {
    buffer = parse(storage()?.getItem(key(scope)) ?? null, now);
  } catch {
    return null;
  }
  if (!buffer) {
    dropSheetRecovery(scope);
    return null;
  }
  let base = current;
  let local = current;
  for (const { name } of sheetFields) {
    const change = buffer.changes[name];
    if (!change) continue;
    base = withField(base, name, change.base);
    local = withField(local, name, change.value);
  }
  const merged = mergeSheet(
    base, local, current, buffer.conflicts,
    Object.keys(buffer.numericText) as SheetField[],
  );
  return {
    ...merged,
    numericText: buffer.numericText,
    editedAt: buffer.editedAt,
  };
}
