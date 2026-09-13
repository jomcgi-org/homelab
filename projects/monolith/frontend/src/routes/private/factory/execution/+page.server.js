// No URL fallback: API_BASE is injected via values.yaml in prod; a localhost
// dev run sets it in the environment. A missing var fails loudly instead of
// silently serving from the wrong backend.
//
// The offered model catalogue (GET /api/agents/models, issue #4859) loads
// here alongside the sessions list. It is passed through untouched: the page
// has no bundled fallback list, so an empty response renders an empty picker.
const API_BASE = process.env.API_BASE;

export async function load({ fetch }) {
  try {
    const [sessionsRes, modelsRes] = await Promise.all([
      fetch(`${API_BASE}/api/agents/sessions`, {
        signal: AbortSignal.timeout(15000),
      }),
      fetch(`${API_BASE}/api/agents/models`, {
        signal: AbortSignal.timeout(15000),
      }),
    ]);
    if (!sessionsRes.ok || !modelsRes.ok) {
      return { sessions: [], models: [], error: true };
    }
    const sessions = await sessionsRes.json();
    const models = await modelsRes.json();
    return {
      sessions: Array.isArray(sessions) ? sessions : (sessions.sessions ?? []),
      models: Array.isArray(models.models) ? models.models : [],
      error: false,
    };
  } catch {
    return { sessions: [], models: [], error: true };
  }
}
