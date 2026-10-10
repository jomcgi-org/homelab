<script>
  import { onMount } from "svelte";
  import "$lib/grimoire/theme.css";
  let { data, form } = $props();
  let pending = $state(false);
  onMount(() => {
    const resume = (event) => {
      if (event.persisted) window.location.reload();
    };
    window.addEventListener("pageshow", resume);
    return () => window.removeEventListener("pageshow", resume);
  });
  function submit(event) {
    if (pending) {
      event.preventDefault();
      return;
    }
    pending = true;
  }
</script>

<svelte:head
  ><title>Accept invitation · Grimoire</title><meta
    name="robots"
    content="noindex,nofollow"
  /></svelte:head
>

<main class="grimoire join">
  <p>Grimoire</p>
  <h1>Join a campaign</h1>
  {#if form?.error || data.error}
    <p role="alert">{form?.error || data.error}</p>
    <p>Open the original invitation to try again.</p>
  {:else if data.invitation}
    <h2>{data.invitation.campaign_name}</h2>
    <p>Invited email: <strong>{data.invitation.invitee_email}</strong></p>
    <p>Signed in as {data.email}.</p>
    <p>
      Expires {new Date(data.invitation.expires_at).toLocaleString("en-GB", {
        timeZone: "UTC",
      })} UTC.
    </p>
    {#if data.invitation.status === "accepted"}
      <p role="status">This invitation has already been accepted.</p>
      <a href="/grimoire">Open your campaigns</a>
    {:else if data.invitation.status !== "pending"}
      <p role="status">
        This invitation is {data.invitation.status}. Ask the campaign owner for
        a new link.
      </p>
    {:else}
      {#if !data.matches}
        <p>
          The invited email differs from your current account email. Grimoire
          will check whether this is the account the owner invited when you
          accept.
        </p>
        <p>
          If you need another account, <a href="/grimoire/oauth2/logout"
            >sign out</a
          >, then reopen the original invitation.
        </p>
      {/if}
      <p>You will join as a player. This link can be accepted once.</p>
      <form method="POST" action="?/accept" onsubmit={submit}>
        <input type="hidden" name="invitation_id" value={data.invitation.id} />
        <button disabled={pending}
          >{pending ? "Joining…" : "Accept and join campaign"}</button
        >
      </form>
    {/if}
  {/if}
  <form method="POST" action="?/cancel">
    <button class="secondary" disabled={pending}>Close invitation</button>
  </form>
</main>

<style>
  .join {
    max-width: 40rem;
    margin: auto;
    padding: 3rem 1.25rem;
    min-height: 100vh;
  }
  h1,
  h2 {
    font-family: var(--grim-serif);
  }
  h1 {
    font-size: 2.5rem;
    line-height: 1.15;
  }
  a {
    color: var(--grim-accent);
  }
  form {
    margin: 1.5rem 0;
  }
  button {
    font: inherit;
    padding: 0.75rem 1rem;
    border: 1px solid var(--grim-accent);
    border-radius: 0.3rem;
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.65;
    cursor: wait;
  }
  .secondary {
    background: transparent;
    color: inherit;
  }
</style>
