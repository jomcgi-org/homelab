<script>
  // The friends tier is one sheet: the technical-drawing tokens ground every
  // screen (the Grimoire campaign screens read them through the bridge in
  // $lib/grimoire/theme.css), and the reader's persisted scheme applies
  // before paint, as on the public tier.
  import "$lib/public/styles/technical-drawing.css";
  // The session carries its font without the root error stylesheet or a
  // third-party font request. Reuse the licensed face the previews exercise.
  import "$lib/grimoire/fonts.css";
  import SchemePreference from "$lib/public/components/SchemePreference.svelte";
  let { children } = $props();
</script>

<SchemePreference />
<div class="td friends">{@render children()}</div>

<style>
  .friends {
    min-height: 100vh;
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-ui);
    line-height: 1.5;
  }
</style>
