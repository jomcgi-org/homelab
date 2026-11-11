/**
 * Campaign notes: a player's own character notes and the party's shared ones.
 *
 * The backend decides who may read a note. A character note belongs to its
 * author and reaches the DM only when the author shares it; party notes reach
 * the DM and every player holding a character.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { NoteCreateRequest, NotePatchRequest } from "./schema";

export type NoteKind = "character" | "party";

/** A linked entity as this viewer may know it. Hidden entities are absent. */
export type NoteEntity = { id: string; name: string; type: string };

export type Note = {
  id: string;
  kind: NoteKind;
  title: string;
  markdown: string;
  pinned: boolean;
  created_in_session: string | null;
  created_at: string;
  updated_at: string;
  is_mine: boolean;
  can_edit: boolean;
  links: { entities: NoteEntity[]; event_ids: string[] };
  /** Present only for the note's author. */
  dm_readable?: boolean;
  /** Present only for the author and the DM. */
  author_member_id?: string | null;
  player_character_id?: string | null;
};

export const noteKeys = {
  all: (campaignId: string) => ["campaign", campaignId, "notes"] as const,
  list: (campaignId: string, kind: NoteKind, q: string) =>
    [...noteKeys.all(campaignId), "list", kind, q] as const,
  one: (campaignId: string, noteId: string) =>
    [...noteKeys.all(campaignId), "one", noteId] as const,
};

/** Notes of one kind, pinned first, optionally narrowed by a text search. */
export const useNotes = (
  campaignId: string,
  kind: NoteKind,
  options: { q?: string; refetchInterval?: number } = {},
) =>
  useQuery({
    queryKey: noteKeys.list(campaignId, kind, options.q ?? ""),
    queryFn: async () =>
      (await api("GET /campaigns/{campaign_id}/notes", {
        params: { campaign_id: campaignId },
        query: { kind, ...(options.q ? { q: options.q } : {}) },
      })) as Note[],
    refetchInterval: options.refetchInterval,
  });

/** One note by id, for a link that points at it. Skipped without an id. */
export const useNote = (campaignId: string, noteId: string | null) =>
  useQuery({
    queryKey: noteKeys.one(campaignId, noteId ?? ""),
    enabled: noteId !== null,
    queryFn: async () =>
      (await api("GET /campaigns/{campaign_id}/notes/{note_id}", {
        params: { campaign_id: campaignId, note_id: noteId! },
      })) as Note,
  });

/** Leaving `dm_readable` out applies the campaign's default sharing. */
export const createNote = (input: {
  campaignId: string;
  note: NoteCreateRequest;
}) =>
  api("POST /campaigns/{campaign_id}/notes", {
    params: { campaign_id: input.campaignId },
    body: input.note,
  });

/**
 * Only the fields given change. `dm_readable` may be sent only by the author
 * of a character note; the backend refuses it on any other note.
 */
export const updateNote = (input: {
  campaignId: string;
  noteId: string;
  changes: NotePatchRequest;
}) =>
  api("PATCH /campaigns/{campaign_id}/notes/{note_id}", {
    params: { campaign_id: input.campaignId, note_id: input.noteId },
    body: input.changes,
  });

export const deleteNote = (input: { campaignId: string; noteId: string }) =>
  api("DELETE /campaigns/{campaign_id}/notes/{note_id}", {
    params: { campaign_id: input.campaignId, note_id: input.noteId },
  });

/** Copy a feed event into the viewer's character notes. */
export const pinEvent = (input: { campaignId: string; eventId: string }) =>
  api("POST /campaigns/{campaign_id}/notes", {
    params: { campaign_id: input.campaignId },
    body: { kind: "character", from_event_id: input.eventId },
  });
