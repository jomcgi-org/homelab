<!--
  Bad case for no-shallow-routing-for-url-state.
  Selection state is derived from page.url.searchParams, but navigation goes
  through pushState/replaceState, which never reassign page.url. The derived
  value therefore never changes and the effect never runs.
-->
<script>
  // ruleid: no-shallow-routing-for-url-state
  import { pushState, replaceState } from "$app/navigation";
  import { page } from "$app/stores";

  const selectedId = $derived($page.url.searchParams.get("session"));

  function select(id) {
    pushState(`${$page.url.pathname}?session=${id}`, {});
  }

  function clear() {
    replaceState($page.url.pathname, {});
  }
</script>

<button onclick={() => select("abc")}>select</button>
<button onclick={clear}>clear</button>
<p>{selectedId}</p>
