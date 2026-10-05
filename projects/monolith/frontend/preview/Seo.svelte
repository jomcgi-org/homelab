<script>
  let { title, description } = $props();
</script>

<svelte:head>
  <title>{title} · synthetic preview</title>
  <meta name="description" content={description} />
  <meta name="robots" content="noindex,nofollow" />
</svelte:head>
