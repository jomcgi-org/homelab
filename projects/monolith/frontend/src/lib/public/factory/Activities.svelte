<script>
  // What a turn ran, one row per recorded tool call: a command, an edit or a
  // write, or a named tool. A long command opens in place; an edit opens the
  // file's slice of the turn diff. Rows past the limit sit behind one toggle,
  // or behind a link to the session record when the task page is summarising.
  import Diff from "./Diff.svelte";
  import { fileFor, splitPath } from "./diff.js";
  import { activityRow, activitySummary, plural } from "./activity-view.js";

  let { activities = [], diff = null, limit = 12, more = null } = $props();

  // A command longer than this, or with a second line, folds to its first
  // line and opens on click.
  const FOLD_AT = 140;

  let open = $state({});
  let all = $state(false);

  const summary = $derived(activitySummary(activities, 0));
  const rows = $derived(
    activities.map((activity) => {
      const row = activityRow(activity);
      const file = row.path ? fileFor(diff, row.path) : null;
      const text = row.what ?? "";
      const firstLine = text.split("\n")[0];
      const folds = text.length > FOLD_AT || firstLine.length < text.length;
      return {
        ...row,
        file,
        head: folds ? `${firstLine.slice(0, FOLD_AT)}…` : text,
        folds,
        parts: row.path ? splitPath(row.path) : null,
      };
    }),
  );
  const shown = $derived(
    all || rows.length <= limit ? rows : rows.slice(0, limit),
  );
  const hidden = $derived(rows.length - shown.length);

  const toggle = (key) => (open[key] = !open[key]);
</script>

{#if rows.length}
  <div class="acts">
    <p class="did">
      {#each summary.counts as count (count.kind)}
        <span>{plural(count.count, count.kind)}</span>
      {/each}
    </p>
    <ul class="act-rows">
      {#each shown as row, index (index)}
        {@const opens = row.folds || row.file}
        <li class:open={open[index]}>
          {#if opens}
            <button
              class="a x"
              type="button"
              aria-expanded={Boolean(open[index])}
              onclick={() => toggle(index)}
            >
              <span class="ty">{row.type}</span>
              <span class="what"
                >{#if row.parts}<span class="dir"
                    >{#each row.parts.dir.split("/") as seg, segIndex (segIndex)}{#if segIndex}/<wbr
                        />{/if}{seg}{/each}</span
                  ><span class="name">{row.parts.name}</span>{:else}{open[index]
                    ? row.what
                    : row.head}{/if}</span
              >
              <span class="tog" aria-hidden="true"
                >{open[index] ? "−" : "+"}</span
              >
            </button>
            {#if open[index] && row.file}
              <div class="opened">
                <Diff files={[row.file]} open={true} />
              </div>
            {/if}
          {:else}
            <div class="a">
              <span class="ty">{row.type}</span>
              <span class="what"
                >{#if row.parts}<span class="dir"
                    >{#each row.parts.dir.split("/") as seg, segIndex (segIndex)}{#if segIndex}/<wbr
                        />{/if}{seg}{/each}</span
                  ><span class="name">{row.parts.name}</span
                  >{:else}{row.what}{/if}</span
              >
              <span class="tog" aria-hidden="true"></span>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
    {#if hidden > 0}
      {#if more}
        <a class="more" href={more}>{hidden} more in the session ›</a>
      {:else}
        <button
          class="more-tog"
          type="button"
          aria-expanded={all}
          onclick={() => (all = !all)}>{hidden} more +</button
        >
      {/if}
    {:else if all && rows.length > limit}
      <button
        class="more-tog"
        type="button"
        aria-expanded={all}
        onclick={() => (all = false)}>fewer −</button
      >
    {/if}
  </div>
{/if}
