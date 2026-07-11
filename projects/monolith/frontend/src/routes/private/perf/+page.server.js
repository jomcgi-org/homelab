const API_BASE = process.env.API_BASE;

export async function load({ fetch }) {
  try {
    const res = await fetch(`${API_BASE}/api/semgrep/perf?limit=300`, {
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      return {
        comparisons: [],
        aggregates: null,
        windowStart: null,
        note: "",
        counts: null,
        error: res.status,
      };
    }
    const body = await res.json();
    return {
      comparisons: body.comparisons ?? [],
      aggregates: body.aggregates ?? null,
      windowStart: body.window_start ?? null,
      note: body.coverage_note ?? "",
      counts: body.counts ?? null,
    };
  } catch (e) {
    return {
      comparisons: [],
      aggregates: null,
      windowStart: null,
      note: "",
      counts: null,
      error: "unavailable",
    };
  }
}
