<script>
  import { onMount } from "svelte";
  let { campaign, form, canEnroll = false, isAdmin = false } = $props();
  let pending = $state(false);
  let copyMessage = $state("");
  let dismissed = $state(false);
  let linkInput = $state();
  onMount(() => {
    const forget = () => {
      dismissed = true;
      copyMessage = "";
    };
    const resume = () => {
      pending = false;
    };
    window.addEventListener("pagehide", forget);
    window.addEventListener("pageshow", resume);
    return () => {
      window.removeEventListener("pagehide", forget);
      window.removeEventListener("pageshow", resume);
    };
  });
  const created = $derived(
    form?.new_link?.campaign_id === campaign.id && !dismissed
      ? form.new_link
      : null,
  );

  function submit(event) {
    if (pending) {
      event.preventDefault();
      return;
    }
    pending = true;
  }
  async function copyLink() {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.url);
      copyMessage = "Link copied. Share it privately with the invited player.";
    } catch {
      linkInput?.focus();
      linkInput?.select();
      copyMessage =
        "Copy is unavailable here. Select the link and copy it manually.";
    }
  }
</script>

<section aria-label="Single-use campaign links">
  <h4>Invite a player with a link</h4>
  <p>
    Each link is for one registered player and can be accepted once. Share it
    privately with that player.
  </p>
  <form method="POST" action="?/createLink" onsubmit={submit}>
    <input type="hidden" name="campaign_id" value={campaign.id} />
    <label
      >Player's email or @username <input
        type="text"
        name="email"
        required
        maxlength="320"
        autocomplete="off"
      /></label
    >
    {#if isAdmin && canEnroll}
      <label class="check"
        ><input type="checkbox" name="allow_enrollment" /> Allow this player to create
        a Grimoire account</label
      >
    {:else if isAdmin}
      <p>
        New-account invitations are unavailable right now. You can create a link
        for someone who has already signed in to Grimoire.
      </p>
    {:else}
      <p>The player must already have signed in to Grimoire.</p>
    {/if}
    <button disabled={pending}
      >{pending ? "Creating link…" : "Create invitation link"}</button
    >
  </form>
  {#if created}
    <div class="created" role="status">
      <p>Link created for <strong>{created.invitee_email}</strong>.</p>
      <p>
        Expires {new Date(created.expires_at).toLocaleString("en-GB", {
          timeZone: "UTC",
        })} UTC. Copy it now; the full link will not be shown again.
      </p>
      <label
        >Private invitation link <input
          bind:this={linkInput}
          readonly
          value={created.url}
          onclick={(event) => event.currentTarget.select()}
        /></label
      >
      <div class="buttons">
        <button type="button" onclick={copyLink}>Copy link</button><button
          type="button"
          class="secondary"
          onclick={() => {
            dismissed = true;
            copyMessage = "";
          }}>Hide link</button
        >
      </div>
      {#if copyMessage}<p aria-live="polite">{copyMessage}</p>{/if}
    </div>
  {/if}
  {#if campaign.join_links?.length}
    <h4>Invitation links</h4>
    <ul>
      {#each campaign.join_links as link (link.id)}
        <li>
          <span
            >{link.invitee_email} · {link.status}<br />Expires {new Date(
              link.expires_at,
            ).toLocaleString("en-GB", { timeZone: "UTC" })} UTC</span
          >
          {#if link.status === "pending" || link.enrollment_cleanup_pending}
            <form method="POST" action="?/revokeLink" onsubmit={submit}>
              <input type="hidden" name="campaign_id" value={campaign.id} />
              <input type="hidden" name="join_link_id" value={link.id} />
              <button class="secondary" disabled={pending}
                >{link.enrollment_cleanup_pending
                  ? "Retry account invitation cleanup"
                  : "Revoke link"}</button
              >
            </form>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  section {
    margin-top: 1.5rem;
    border-top: 1px solid var(--grim-line);
    padding-top: 1rem;
  }
  h4 {
    margin: 0 0 0.75rem;
    font-size: 1.05rem;
  }
  form,
  label {
    display: grid;
    gap: 0.65rem;
  }
  label:not(.check) {
    min-width: 0;
  }
  input:not([type="checkbox"]) {
    box-sizing: border-box;
    width: 100%;
    padding: 0.7rem;
    font: inherit;
    border: 1px solid var(--grim-line);
    border-radius: 0.3rem;
    background: var(--grim-surface);
    color: inherit;
  }
  .check {
    display: flex;
    align-items: center;
  }
  button {
    justify-self: start;
    padding: 0.75rem 1rem;
    border: 1px solid var(--grim-accent);
    border-radius: 0.3rem;
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    font: inherit;
    cursor: pointer;
  }
  .secondary {
    background: transparent;
    color: inherit;
  }
  button:disabled {
    opacity: 0.65;
    cursor: wait;
  }
  .created {
    border-left: 3px solid var(--grim-accent);
    padding: 1rem;
    margin: 1rem 0;
    background: var(--grim-paper);
  }
  .buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    margin-top: 0.75rem;
  }
  ul {
    padding: 0;
    list-style: none;
  }
  li {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    margin: 1rem 0;
  }
</style>
