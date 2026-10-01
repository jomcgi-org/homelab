<script>
  // A unified diff as one disclosure per file: the file header is the summary
  // (its path and its line counts), the hunks are the body. Small diffs open
  // on arrival; a patch that rewrote half the repo arrives closed, so the
  // list of files is the first thing read rather than the last.
  import { parseDiff, splitPath } from "./diff.js";

  let { diff = null, files = null, truncated = false, open = null } = $props();

  // Below this many changed lines every file opens by itself.
  const OPEN_BELOW = 160;
  const KIND_WORD = {
    added: "new",
    deleted: "deleted",
    renamed: "renamed",
    binary: "binary",
    modified: "",
  };

  const parsed = $derived(files ? { files } : parseDiff(diff));
  const total = $derived(
    parsed.files.reduce(
      (sum, file) => sum + file.additions + file.deletions,
      0,
    ),
  );
  const openAll = $derived(open ?? total <= OPEN_BELOW);
</script>

<div class="diff">
  {#each parsed.files as file, index (index)}
    {@const path = splitPath(file.path || file.oldPath || "(unnamed)")}
    <details class="dfile" open={openAll}>
      <summary>
        <span class="tog" aria-hidden="true"></span>
        <span class="path"
          ><span class="dir"
            >{#each path.dir.split("/") as seg, segIndex (segIndex)}{#if segIndex}/<wbr
                />{/if}{seg}{/each}</span
          ><span class="name">{path.name}</span
          >{#if file.kind === "renamed"}<span class="from"
              >← {file.oldPath}</span
            >{/if}</span
        >
        {#if KIND_WORD[file.kind]}<span class="kind"
            >{KIND_WORD[file.kind]}</span
          >{/if}
        <span class="stat num"
          ><b>+{file.additions}</b> <s>−{file.deletions}</s></span
        >
      </summary>
      <div class="hunks">
        {#if file.kind === "binary"}
          <div class="ln note">
            <span></span><span></span><span>binary</span>
          </div>
        {/if}
        {#each file.hunks as hunk, hunkIndex (hunkIndex)}
          <div class="ln hd">
            <span></span><span></span><span>{hunk.header}</span>
          </div>
          {#each hunk.lines as line, lineIndex (lineIndex)}
            <div class="ln {line.kind}">
              <span class="no">{line.old ?? ""}</span><span class="no"
                >{line.new ?? ""}</span
              ><span class="tx">{line.text}</span>
            </div>
          {/each}
        {/each}
      </div>
    </details>
  {/each}
  {#if truncated}
    <p class="cut">diff truncated at 256 KiB</p>
  {/if}
</div>
