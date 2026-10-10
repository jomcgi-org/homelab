import { error } from "@sveltejs/kit";
import { grimoireJson } from "./grimoire-auth.js";

export async function sessionState(
  fetch,
  cookies,
  campaignId,
  selectedSessionId = null,
) {
  const lobby = await grimoireJson(fetch, cookies, "/lobby");
  const campaign = lobby.campaigns.find((row) => row.id === campaignId);
  if (!campaign) error(403, "You are not a member of this campaign.");
  const base = `/campaigns/${campaignId}`;
  const [characters, sessions, voices] = await Promise.all([
    grimoireJson(fetch, cookies, `${base}/characters`),
    grimoireJson(fetch, cookies, `${base}/sessions`),
    grimoireJson(fetch, cookies, `${base}/voices`),
  ]);
  const session = selectedSessionId
    ? sessions.find((row) => row.id === selectedSessionId)
    : sessions[0] || null;
  if (selectedSessionId && !session) error(404, "Session not found.");
  const sheets = await Promise.all(
    characters.map(async (character) => {
      const history = await grimoireJson(
        fetch,
        cookies,
        `${base}/characters/${character.id}/sheets`,
      );
      const approved = history.versions
        ?.filter((version) => version.status === "approved")
        .sort((a, b) => b.version - a.version)[0];
      return { ...character, approved: approved?.derived || null };
    }),
  );
  const events = [];
  let journal = null;
  if (session) {
    let after = 0;
    for (;;) {
      const batch = await grimoireJson(
        fetch,
        cookies,
        `${base}/sessions/${session.id}/events?after=${after}&limit=500`,
      );
      events.push(...batch);
      if (batch.length < 500) break;
      after = batch.at(-1).seq;
    }
    const [mine, party] = await Promise.all([
      grimoireJson(fetch, cookies, `${base}/sessions/${session.id}/journal`),
      grimoireJson(
        fetch,
        cookies,
        `${base}/sessions/${session.id}/journal?view=party`,
      ),
    ]);
    journal = { mine: sessionJournal(mine), party: sessionJournal(party) };
  }
  const dmData =
    campaign.role === "dm"
      ? {
          members: await grimoireJson(fetch, cookies, `${base}/members`),
          npcs: await campaignNpcs(fetch, cookies, base),
        }
      : {};
  return {
    campaign,
    characters: sheets,
    session,
    events,
    journal,
    voices,
    user: lobby.user,
    ...(selectedSessionId ? { selectedSessionId } : {}),
    ...dmData,
  };
}

// The backend journal is already the viewer's audience-filtered projection, so
// entries pass through untouched. Only the known sections survive, each
// defaulting to empty, so the panel never meets a missing list.
const journalSections = [
  "learned",
  "received",
  "people_and_places",
  "rolls",
  "open_threads",
];

async function campaignNpcs(fetch, cookies, base) {
  const npcs = [];
  let cursor = null;
  do {
    const query = new URLSearchParams({ type: "npc", limit: "500" });
    if (cursor) query.set("cursor", cursor);
    const page = await grimoireJson(
      fetch,
      cookies,
      `${base}/entities?${query}`,
    );
    npcs.push(
      ...(page.items || []).filter((entity) => entity.entity_type === "npc"),
    );
    cursor = page.next_cursor;
  } while (cursor);
  return npcs;
}

function sessionJournal(view) {
  return {
    ...Object.fromEntries(
      journalSections.map((key) => [key, view?.[key] || []]),
    ),
    truncated: view?.truncated === true,
  };
}
