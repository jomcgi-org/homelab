<!--
  Ok case for no-shallow-routing-for-url-state: the remedy.

  Selection is still derived from page.url.searchParams, but navigation goes
  through goto, which runs a real navigation and reassigns page.url, so the
  derived value recomputes. noScroll holds the scroll position and keepFocus
  leaves focus alone for components that manage it themselves.
-->
<script>
  // ok: no-shallow-routing-for-url-state
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";

  const selectedId = $derived($page.url.searchParams.get("session"));

  function select(id) {
    goto(`${$page.url.pathname}?session=${id}`, {
      noScroll: true,
      keepFocus: true,
    });
  }
</script>

<button onclick={() => select("abc")}>select</button>
<p>{selectedId}</p>
