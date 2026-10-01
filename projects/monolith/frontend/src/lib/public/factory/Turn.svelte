<script>
  // One turn of a session: the instruction it was given, what it ran, what it
  // replied, and the line of numbers under it. The task page and the session
  // record both render this; `full` is the record, which also carries the
  // patch and the whole activity list.
  import Activities from "./Activities.svelte";
  import Diff from "./Diff.svelte";
  import Markdown from "./Markdown.svelte";
  import { parseDiff } from "./diff.js";
  import {
    clip,
    commitUrl,
    plural,
    stripRationale,
    tokens,
    turnMeta,
  } from "./activity-view.js";

  let { turn, full = false, session = null } = $props();

  // A reply past this many characters opens on a toggle; the task page is a
  // walkthrough and a 20k-character review would bury the steps after it.
  const REPLY_CLIP = 5000;
  // The instruction preview: enough of its first line to say what it was.
  const ASK_PREVIEW = 160;
  // How many activity rows the task page shows before pointing at the record.
  const DIGEST_ROWS = 6;

  let replyOpen = $state(false);

  const prompt = $derived(turn.prompt ?? "");
  const askPreview = $derived(
    prompt.replace(/\s+/g, " ").trim().slice(0, ASK_PREVIEW),
  );
  const reply = $derived(stripRationale(turn.result_text, turn.rationale));
  const cut = $derived(clip(reply, REPLY_CLIP));
  const parsed = $derived(full && turn.diff ? parseDiff(turn.diff) : null);
  const meta = $derived(turnMeta(turn));
  const activities = $derived(turn.activities ?? []);
</script>

<div class="turn">
  <span class="tn">{turn.seq}</span>
  <div class="body">
    {#if prompt}
      <details class="ask">
        <summary>
          <span class="ty">prompt</span>
          <span class="preview">{askPreview}</span>
          <span class="len num">{tokens(prompt.length)} chars</span>
        </summary>
        <pre class="ask-text">{prompt}</pre>
      </details>
    {/if}

    {#if activities.length}
      <Activities
        {activities}
        diff={parsed}
        limit={full ? 12 : DIGEST_ROWS}
        more={full ? null : session}
      />
    {/if}

    {#if reply}
      <div class="reply">
        <Markdown text={cut.clipped && !replyOpen ? cut.head : reply} />
        {#if cut.clipped}
          <button
            class="more-tog"
            type="button"
            aria-expanded={replyOpen}
            onclick={() => (replyOpen = !replyOpen)}
            >{replyOpen ? "fewer −" : `${tokens(reply.length)} chars +`}</button
          >
        {/if}
      </div>
    {:else}
      <p class="none">no reply recorded</p>
    {/if}

    {#if full && turn.rationale?.raw}
      <div class="why">
        <span class="ty">rationale</span>
        <pre>{turn.rationale.raw}</pre>
      </div>
    {/if}

    <div class="meta">
      {#each meta as part, index (index)}
        {#if part.sha}
          <span
            >{part.text}<a class="sha" href={commitUrl(part.sha)}
              >{part.sha.slice(0, 10)}</a
            >{#if full && part.baseSha}
              on <a class="sha" href={commitUrl(part.baseSha)}
                >{part.baseSha.slice(0, 10)}</a
              >{/if}</span
          >
        {:else if part.stat}
          <span class="stat"
            >{plural(part.stat.files, "file")}
            <b>+{part.stat.additions}</b>
            <s>−{part.stat.deletions}</s></span
          >
        {:else if part.strong}
          <span>{part.text}<b>{part.strong}</b></span>
        {:else if part.bad}
          <span class="bad">{part.text}</span>
        {:else if full || index === 0}
          <span>{part.text}</span>
        {/if}
      {/each}
    </div>

    {#if parsed?.files.length}
      <Diff files={parsed.files} truncated={turn.diff_truncated} />
    {/if}
  </div>
</div>
