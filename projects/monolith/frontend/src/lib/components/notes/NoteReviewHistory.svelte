<script>
  let { history } = $props();

  function date(value) {
    if (!value) return "unrecorded";
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf())
      ? "unrecorded"
      : parsed
          .toISOString()
          .replace("T", " ")
          .replace(/\.\d{3}Z$/, " UTC");
  }
</script>

{#if history}
  <details class="review-history">
    <summary>Review history ({history.entries.length})</summary>
    <p>Stored attempts. Current freshness is shown above.</p>
    {#if history.entries.length === 0}
      <p>No review attempts recorded.</p>
    {:else}
      <ol>
        {#each history.entries as entry (entry.id)}
          <li>
            <strong>{entry.status}</strong>
            <time datetime={entry.attempted_at}>{date(entry.attempted_at)}</time
            >
            <p>
              Revision {entry.note_revision}:
              {entry.is_current_revision
                ? "current claim"
                : "other claim revision"}. Reviewer/lane: {entry.reviewer_lane ??
                "unrecorded"}.
            </p>
            <p>{entry.reason}{entry.reason_truncated ? "… (truncated)" : ""}</p>
            <p>
              Evidence observed: {date(entry.evidence_observed_at)}. Next
              attempt recorded: {date(entry.next_attempt_at)}.
            </p>
          </li>
        {/each}
      </ol>
      {#if history.has_more}
        <p>
          Showing the newest {history.entries.length} attempts. Older attempts are
          omitted.
        </p>
      {/if}
    {/if}
  </details>
{/if}

<style>
  .review-history {
    margin-top: 16px;
    border-top: 1px dashed currentColor;
    padding-top: 10px;
    font-size: 11px;
    overflow-wrap: anywhere;
  }
  summary {
    cursor: pointer;
    font-weight: 700;
    margin-bottom: 8px;
  }
  summary:focus-visible {
    outline: 1px solid currentColor;
    outline-offset: 3px;
  }
  li {
    margin-bottom: 12px;
  }
  time {
    display: block;
  }
</style>
