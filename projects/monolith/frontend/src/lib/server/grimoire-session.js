import { error } from "@sveltejs/kit";
import { grimoireJson } from "./grimoire-auth.js";

export async function sessionState(fetch, cookies, campaignId) {
  const lobby = await grimoireJson(fetch, cookies, "/lobby");
  const campaign = lobby.campaigns.find((row) => row.id === campaignId);
  if (!campaign) error(403, "You are not a member of this campaign.");
  const base = `/campaigns/${campaignId}`;
  const [characters, sessions] = await Promise.all([
    grimoireJson(fetch, cookies, `${base}/characters`),
    grimoireJson(fetch, cookies, `${base}/sessions`),
  ]);
  const session = sessions[0] || null;
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
      ? { members: await grimoireJson(fetch, cookies, `${base}/members`) }
      : {};
  return {
    campaign,
    characters: sheets,
    session,
    events,
    journal,
    user: lobby.user,
    ...dmData,
  };
}

function sessionJournal(view) {
  const event = (entry) => ({ ...entry.body, event_id: entry.id });
  return {
    ...view,
    learned: (view.learned || []).map((entry) => ({
      ...entry,
      projection: entry.entity,
    })),
    people_places: (view.people_and_places || []).map((entry) => ({
      ...entry,
      entity_id: entry.id,
    })),
    received: (view.received || []).map(event),
    rolls: (view.rolls || []).map(event),
    open_threads: (view.open_threads || []).map(event),
  };
}
