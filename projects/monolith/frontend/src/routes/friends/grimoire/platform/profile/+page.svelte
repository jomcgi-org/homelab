<script>
  let { data } = $props();
</script>

<svelte:head><title>Your platform account</title></svelte:head>
<main>
  <h1>Welcome, {data.user.display_name}</h1>
  <p>Your account is registered as {data.user.username}.</p>
  {#if data.user.permissions.includes("grimoire.access")}
    <a href="/grimoire">Open Grimoire</a>
  {:else}
    <p>
      Ask your administrator to enable Grimoire access for your account.
      Campaign invitations are separate.
    </p>
    <p>Your platform user ID: {data.user.id}</p>
  {/if}
</main>

<style>
  main {
    max-width: 640px;
    margin: 4rem auto;
    padding: 1rem;
  }
</style>
