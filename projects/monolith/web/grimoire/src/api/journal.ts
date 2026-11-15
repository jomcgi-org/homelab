/**
 * The journal: what a viewer learned, received and rolled, computed by the
 * backend from the events they may see. Entries arrive already filtered for
 * the viewer; nothing here widens or reshapes them.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { Journal } from "./schema";

export type JournalView = "mine" | "party";

/** A reveal the viewer saw. A retracted entry keeps its identity only. */
export interface LearnedEntry {
  event_id?: string;
  entity_id: string;
  name?: string | null;
  entity_type?: string | null;
  grant_scope?: string | null;
  retracted?: boolean;
  /** Only the DM is told which character learned it. */
  player_character_id?: string;
  /** The snapshot shown at the time of the reveal. */
  entity?: Record<string, unknown> & { recognition_only?: boolean };
}

/** A session event as the journal projects it. */
export interface JournalEvent {
  id: string;
  kind?: string;
  body?: Record<string, unknown> | null;
}

export interface JournalEntity {
  id: string;
  name: string;
  entity_type?: string | null;
}

export interface JournalSections {
  learned: LearnedEntry[];
  received: JournalEvent[];
  people_and_places: JournalEntity[];
  rolls: JournalEvent[];
  open_threads: JournalEvent[];
  /** The session had more events than one read folds; later ones are missing. */
  truncated: boolean;
}

const list = <T>(value: unknown): T[] =>
  Array.isArray(value) ? (value as T[]) : [];

/** Every section present, so a reader never meets a missing list. */
export const journalSections = (journal: Journal): JournalSections => ({
  learned: list(journal.learned),
  received: list(journal.received),
  people_and_places: list(journal.people_and_places),
  rolls: list(journal.rolls),
  open_threads: list(journal.open_threads),
  truncated: journal.truncated === true,
});

export const journalKeys = {
  campaign: (campaignId: string, view: JournalView) =>
    ["campaign", campaignId, "journal", view] as const,
};

/** Every session's journal, newest first, ten sessions to a page. */
export const useCampaignJournal = (
  campaignId: string,
  view: JournalView,
  enabled = true,
) =>
  useInfiniteQuery({
    queryKey: journalKeys.campaign(campaignId, view),
    enabled,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      api("GET /campaigns/{campaign_id}/journal", {
        params: { campaign_id: campaignId },
        query: { view, cursor: pageParam },
      }),
    getNextPageParam: (page) => page.next_cursor,
  });
