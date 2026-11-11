/**
 * The live table: sessions, the event feed and the commands a seat can send.
 *
 * Polling is tiered. The feed asks only for events after the last one it
 * holds, every two seconds while the tab is visible. Slower reads (characters,
 * sheets, voices, members, the lobby) refresh every fifteen seconds, and the
 * journal and turn order refresh when an event that changes them arrives.
 */
import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { keys } from "./lobby";
import type {
  GameSessionView,
  RollRequest,
  SessionEventRequest,
  SessionEventView,
} from "./schema";

export type SessionEvent = SessionEventView;
export type TurnOrder = {
  entries?: Array<{
    label: string;
    player_character_id?: string | null;
    initiative?: number;
    hidden?: boolean;
  }>;
  active_index?: number | null;
  round?: number;
  hidden_display?: "mask" | "omit";
} | null;

const FEED_MS = 2_000;
const SLOW_MS = 15_000;
/** Retractions edit old events in place, so the feed is re-read in full this often. */
const FULL_RESYNC_MS = 15_000;
const PAGE = 500;

export const sessionKeys = {
  sessions: (campaignId: string) =>
    ["campaign", campaignId, "sessions"] as const,
  sheets: (campaignId: string, characterId: string) =>
    ["campaign", campaignId, "sheets", characterId] as const,
  voices: (campaignId: string) => ["campaign", campaignId, "voices"] as const,
  session: (campaignId: string, sessionId: string) =>
    ["campaign", campaignId, "session", sessionId] as const,
  events: (campaignId: string, sessionId: string) =>
    ["campaign", campaignId, "session", sessionId, "events"] as const,
  initiative: (campaignId: string, sessionId: string) =>
    ["campaign", campaignId, "session", sessionId, "initiative"] as const,
  journal: (campaignId: string, sessionId: string, view: "mine" | "party") =>
    ["campaign", campaignId, "session", sessionId, "journal", view] as const,
};

export const useSessions = (campaignId: string) =>
  useQuery({
    queryKey: sessionKeys.sessions(campaignId),
    queryFn: () =>
      api("GET /campaigns/{campaign_id}/sessions", {
        params: { campaign_id: campaignId },
      }),
    refetchInterval: SLOW_MS,
  });

export const useVoices = (campaignId: string) =>
  useQuery({
    queryKey: sessionKeys.voices(campaignId),
    queryFn: () =>
      api("GET /campaigns/{campaign_id}/voices", {
        params: { campaign_id: campaignId },
      }),
    refetchInterval: SLOW_MS,
  });

/** The numbers the table shows from a character's latest approved sheet. */
export interface ApprovedSheet {
  max_hit_points?: number;
  unarmored_armor_class?: number;
  ability_modifiers?: Record<string, number>;
  [key: string]: unknown;
}

async function eventsAfter(
  campaignId: string,
  sessionId: string,
  after: number,
): Promise<SessionEvent[]> {
  const found: SessionEvent[] = [];
  for (;;) {
    const page = await api(
      "GET /campaigns/{campaign_id}/sessions/{session_id}/events",
      {
        params: { campaign_id: campaignId, session_id: sessionId },
        query: { after: found.at(-1)?.seq ?? after, limit: PAGE },
      },
    );
    found.push(...page);
    if (page.length < PAGE) return found;
  }
}

/**
 * The session's events, oldest first. Each poll fetches only what is newer
 * than the last event held; a periodic full read picks up retractions.
 */
export function useEvents(campaignId: string, session: GameSessionView | null) {
  const client = useQueryClient();
  const sessionId = session?.id ?? "";
  const lastFull = useRef(0);
  const query = useQuery({
    queryKey: sessionKeys.events(campaignId, sessionId),
    enabled: session !== null,
    queryFn: async (): Promise<SessionEvent[]> => {
      const held =
        client.getQueryData<SessionEvent[]>(
          sessionKeys.events(campaignId, sessionId),
        ) ?? [];
      const full = Date.now() - lastFull.current >= FULL_RESYNC_MS;
      if (full || held.length === 0) {
        const all = await eventsAfter(campaignId, sessionId, 0);
        lastFull.current = Date.now();
        return all;
      }
      const fresh = await eventsAfter(campaignId, sessionId, held.at(-1)!.seq);
      return fresh.length ? [...held, ...fresh] : held;
    },
    // An ended session gains no events. Polling pauses while the tab is hidden
    // and resumes with an immediate read when it is shown again.
    refetchInterval: session && session.status !== "ended" ? FEED_MS : false,
    staleTime: 0,
  });

  // The journal and the DM's turn order change only when an event arrives.
  const lastSeq = query.data?.at(-1)?.seq ?? 0;
  const seen = useRef(0);
  useEffect(() => {
    if (!sessionId || lastSeq === seen.current) return;
    const arrived = (query.data ?? []).filter(
      (event) => event.seq > seen.current,
    );
    const first = seen.current === 0;
    seen.current = lastSeq;
    if (first) return;
    client.invalidateQueries({
      queryKey: [...sessionKeys.session(campaignId, sessionId), "journal"],
    });
    if (arrived.some((event) => event.kind === "turn"))
      client.invalidateQueries({
        queryKey: sessionKeys.initiative(campaignId, sessionId),
      });
  }, [client, campaignId, sessionId, lastSeq, query.data]);

  return query;
}

/** The DM's view of the turn order, hidden entries included. */
export const useInitiative = (
  campaignId: string,
  session: GameSessionView | null,
) =>
  useQuery({
    queryKey: sessionKeys.initiative(campaignId, session?.id ?? ""),
    enabled: session !== null,
    queryFn: async (): Promise<TurnOrder> =>
      (await api(
        "GET /campaigns/{campaign_id}/sessions/{session_id}/initiative",
        { params: { campaign_id: campaignId, session_id: session!.id } },
      )) as TurnOrder,
    refetchInterval: SLOW_MS,
  });

export const useJournal = (
  campaignId: string,
  session: GameSessionView | null,
  view: "mine" | "party",
  enabled = true,
) =>
  useQuery({
    queryKey: sessionKeys.journal(campaignId, session?.id ?? "", view),
    enabled: enabled && session !== null,
    queryFn: () =>
      api("GET /campaigns/{campaign_id}/sessions/{session_id}/journal", {
        params: { campaign_id: campaignId, session_id: session!.id },
        query: { view },
      }),
    refetchInterval: SLOW_MS * 2,
  });

// --- Commands --------------------------------------------------------------

export const startSession = (campaignId: string) =>
  api("POST /campaigns/{campaign_id}/sessions", {
    params: { campaign_id: campaignId },
  });

export const setSessionStatus = (input: {
  campaignId: string;
  sessionId: string;
  status: GameSessionView["status"];
}) =>
  api("PATCH /campaigns/{campaign_id}/sessions/{session_id}", {
    params: { campaign_id: input.campaignId, session_id: input.sessionId },
    body: { status: input.status },
  });

export const rollDice = (input: {
  campaignId: string;
  sessionId: string;
  roll: RollRequest;
}) =>
  api("POST /campaigns/{campaign_id}/sessions/{session_id}/rolls", {
    params: { campaign_id: input.campaignId, session_id: input.sessionId },
    // A bare "d20" is shorthand for one die.
    body: { ...input.roll, formula: input.roll.formula.replace(/^d/i, "1d") },
  });

export const postEvent = (input: {
  campaignId: string;
  sessionId: string;
  event: SessionEventRequest;
}) =>
  api("POST /campaigns/{campaign_id}/sessions/{session_id}/events", {
    params: { campaign_id: input.campaignId, session_id: input.sessionId },
    body: input.event,
  });

/** What a command should refetch: the feed now, and whatever it touched. */
export const afterCommand = (campaignId: string, sessionId?: string) => [
  ...(sessionId ? [sessionKeys.session(campaignId, sessionId)] : []),
  sessionKeys.sessions(campaignId),
];

export { keys as lobbyKeys };
