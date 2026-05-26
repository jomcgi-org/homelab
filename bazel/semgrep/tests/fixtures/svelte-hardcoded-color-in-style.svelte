<!-- Test fixtures for the svelte-hardcoded-color-in-style semgrep rule.

Annotations:
  ruleid: svelte-hardcoded-color-in-style   — the next non-annotation line MUST be flagged
  ok: svelte-hardcoded-color-in-style       — the next non-annotation line MUST NOT be flagged
-->

<script>
  // Script section — no CSS, so no color flags here.
  const label = "hello";
</script>

<template>
  <!-- Template section: hex in href anchors should not fire -->
  <!-- ok: svelte-hardcoded-color-in-style -->
  <a href="#section-1">Jump</a>
  <!-- ok: svelte-hardcoded-color-in-style -->
  <p>Some text with #hashtag mention</p>
</template>

<style>
  /* Positive cases — hard-coded hex colors that bypass design tokens */

  /* ruleid: svelte-hardcoded-color-in-style */
  background: #f1ebdc;

  /* ruleid: svelte-hardcoded-color-in-style */
  color: #141414;

  /* ruleid: svelte-hardcoded-color-in-style */
  border-color: #aabbcc;

  /* ruleid: svelte-hardcoded-color-in-style */
  fill: #fff;

  /* ruleid: svelte-hardcoded-color-in-style */
  stroke: #123456ab;

  /* Negative cases — using CSS custom properties (correct pattern) */

  /* ok: svelte-hardcoded-color-in-style */
  background: var(--bg);

  /* ok: svelte-hardcoded-color-in-style */
  color: var(--fg);

  /* ok: svelte-hardcoded-color-in-style */
  border-color: var(--border);

  /* ok: svelte-hardcoded-color-in-style */
  font-family: var(--font-mono);

  /* ok: svelte-hardcoded-color-in-style */
  background: inherit;

  /* ok: svelte-hardcoded-color-in-style */
  color: transparent;
</style>
