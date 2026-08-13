<!--
  Ok cases for no-shallow-routing-for-url-state.

  Genuine shallow routing: pushState carries page.state and the component reads
  page.state, never page.url.searchParams. page.url staying stale is the point
  of the API here, not a bug, so this must not fire.
-->
<script>
  // ok: no-shallow-routing-for-url-state
  import { pushState } from "$app/navigation";
  import { page } from "$app/stores";

  const photo = $derived($page.state.selectedPhoto);

  function openModal(item) {
    pushState("", { selectedPhoto: item });
  }
</script>

<button onclick={() => openModal({ id: 1 })}>open</button>
{#if photo}
  <dialog open>{photo.id}</dialog>
{/if}
