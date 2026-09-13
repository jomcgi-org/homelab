// The poll interval itself is covered by the $effect cleanup in +page.svelte.
// This module only decides what a ledger fetch outcome means for stage state.

export function decidePoll(outcome, cursor = 0) {
  const status = outcome?.status;
  const current = Number(cursor);
  const held = Number.isFinite(current) ? current : 0;
  if (status === 404) {
    return { forget: true, rows: [], cursor: 0 };
  }
  if (!outcome?.ok) {
    return { forget: false, rows: [], cursor: held };
  }
  const rows = Array.isArray(outcome.rows) ? outcome.rows : [];
  const ids = rows.map((row) => Number(row.id)).filter(Number.isFinite);
  const next = ids.length ? Math.max(held, ...ids) : held;
  return { forget: false, rows, cursor: next };
}
