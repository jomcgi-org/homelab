/** Invented notes data and the routes that serve it in tests and the preview. */
import type { Note } from "../../api/notes";
import type { NoteCreateRequest, NotePatchRequest } from "../../api/schema";
import type { Routes } from "../backend";
import { ids } from "../fixtures";

export const noteIds = {
  map: "a1000000-0000-4000-8000-000000000001",
  rumour: "a1000000-0000-4000-8000-000000000002",
  plan: "a1000000-0000-4000-8000-000000000003",
  shared: "a1000000-0000-4000-8000-000000000004",
  pinnedEvent: "e0000000-0000-4000-8000-0000000000aa",
  ferryman: "f1000000-0000-4000-8000-000000000001",
};

export const note = (
  overrides: Partial<Note> & Pick<Note, "id" | "title">,
): Note => ({
  kind: "character",
  markdown: "",
  pinned: false,
  created_in_session: null,
  created_at: "2026-11-10T19:10:00Z",
  updated_at: "2026-11-10T19:10:00Z",
  is_mine: true,
  can_edit: true,
  dm_readable: false,
  links: { entities: [], event_ids: [] },
  ...overrides,
});

/** What a player with a character sees: their own notes and the party's. */
export const playerNotes: Note[] = [
  note({
    id: noteIds.map,
    title: "The ferryman's map",
    markdown: "Three crossings marked.\nThe northern one is circled twice.",
    pinned: true,
    links: {
      entities: [{ id: noteIds.ferryman, name: "Old Tolliver", type: "npc" }],
      event_ids: [noteIds.pinnedEvent],
    },
  }),
  note({
    id: noteIds.rumour,
    title: "Rumour at the Gilded Eel",
    markdown: "The cellar door is never locked on market days.",
    dm_readable: true,
  }),
  note({
    id: noteIds.plan,
    kind: "party",
    title: "Plan for the cellar",
    markdown: "Brannoch first. Lanterns hooded until we are past the rats.",
    is_mine: false,
    can_edit: false,
    dm_readable: undefined,
  }),
];

/** What the DM sees: character notes shared with them, and party notes. */
export const dmNotes: Note[] = [
  note({
    id: noteIds.shared,
    title: "Rumour at the Gilded Eel",
    markdown: "The cellar door is never locked on market days.",
    is_mine: false,
    can_edit: false,
    dm_readable: undefined,
  }),
  note({
    id: noteIds.plan,
    kind: "party",
    title: "Plan for the cellar",
    markdown: "Brannoch first. Lanterns hooded until we are past the rats.",
    is_mine: false,
    dm_readable: undefined,
  }),
];

const missing = { status: 404, detail: "note not found" };

/**
 * A notes backend that remembers writes, so a saved note shows up in the next
 * read. `seed` gives each campaign its starting notes.
 */
export function notesBackend(
  seed: (campaignId: string) => Note[] = (campaignId) =>
    campaignId === ids.owned ? dmNotes : playerNotes,
): Routes {
  const stores = new Map<string, Note[]>();
  const store = (campaignId: string) => {
    if (!stores.has(campaignId)) stores.set(campaignId, [...seed(campaignId)]);
    return stores.get(campaignId)!;
  };
  let created = 0;
  return {
    "GET /campaigns/{campaign_id}/notes": (call) => {
      const kind = call.query.get("kind");
      const q = (call.query.get("q") ?? "").toLowerCase();
      return store(call.params.campaign_id!)
        .filter((row) => !kind || row.kind === kind)
        .filter((row) =>
          `${row.title}\n${row.markdown}`.toLowerCase().includes(q),
        )
        .sort((a, b) => Number(b.pinned) - Number(a.pinned));
    },
    "POST /campaigns/{campaign_id}/notes": (call) => {
      const body = call.body as NoteCreateRequest;
      created += 1;
      const pinnedFrom = body.from_event_id ?? null;
      const row = note({
        id: `a2000000-0000-4000-8000-${String(created).padStart(12, "0")}`,
        kind: body.kind,
        title: body.title || (pinnedFrom ? "Session note" : ""),
        markdown: body.markdown ?? "",
        pinned: pinnedFrom ? true : (body.pinned ?? false),
        dm_readable: body.dm_readable ?? false,
        links: { entities: [], event_ids: pinnedFrom ? [pinnedFrom] : [] },
      });
      store(call.params.campaign_id!).unshift(row);
      return row;
    },
    "GET /campaigns/{campaign_id}/notes/{note_id}": (call) =>
      store(call.params.campaign_id!).find(
        (row) => row.id === call.params.note_id,
      ) ?? missing,
    "PATCH /campaigns/{campaign_id}/notes/{note_id}": (call) => {
      const rows = store(call.params.campaign_id!);
      const index = rows.findIndex((row) => row.id === call.params.note_id);
      if (index < 0) return missing;
      const changes = call.body as NotePatchRequest;
      const next: Note = {
        ...rows[index]!,
        ...(changes.title != null && { title: changes.title }),
        ...(changes.markdown != null && { markdown: changes.markdown }),
        ...(changes.pinned != null && { pinned: changes.pinned }),
        ...(changes.dm_readable != null && {
          dm_readable: changes.dm_readable,
        }),
      };
      rows[index] = next;
      return next;
    },
    "DELETE /campaigns/{campaign_id}/notes/{note_id}": (call) => {
      const rows = store(call.params.campaign_id!);
      const index = rows.findIndex((row) => row.id === call.params.note_id);
      if (index < 0) return missing;
      rows.splice(index, 1);
      return undefined;
    },
  };
}

export const notesRoutes: Routes = notesBackend();
