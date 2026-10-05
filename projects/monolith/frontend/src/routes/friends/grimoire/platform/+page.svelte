<script>
  import { onMount } from "svelte";
  let { data, form } = $props();
  let dismissed = $state(false);
  const newLink = $derived(!dismissed ? form?.link : null);
  function requestId() {
    return crypto.randomUUID();
  }
  onMount(() => {
    const forget = () => {
      dismissed = true;
    };
    window.addEventListener("pagehide", forget);
    return () => window.removeEventListener("pagehide", forget);
  });
</script>

<svelte:head><title>Platform accounts</title></svelte:head>
<main>
  <h1>Platform accounts</h1>
  <p>
    Platform signup and campaign invitations are separate. New accounts have no
    application access until you grant it.
  </p>
  {#if form?.error}<p role="alert">{form.error}</p>{/if}
  {#if newLink}
    <label
      >Private signup link <input
        readonly
        value={newLink}
        onclick={(event) => event.currentTarget.select()}
      /></label
    >
    <p>Copy and share privately. A lost link needs an explicit reissue.</p>
    <button type="button" onclick={() => (dismissed = true)}>Hide link</button>
  {/if}
  {#if data.bootstrapRequired}
    <p>
      An active platform operator account is required. Import your signed
      operator identity explicitly below.
    </p>
    <form method="POST">
      <input type="hidden" name="action" value="bootstrap" />
      <input type="hidden" name="request_id" value={requestId()} />
      <label
        >Existing platform user ID (optional, for explicitly linking your second
        login issuer) <input name="user_id" /></label
      >
      <label>Reason <input name="reason" required maxlength="500" /></label>
      <button>Import my operator identity</button>
    </form>
  {:else}
    <h2>Invite a friend</h2>
    <form method="POST">
      <input type="hidden" name="action" value="issue" />
      <input type="hidden" name="request_id" value={requestId()} />
      <label
        >Friend's name <input
          name="recipient_label"
          required
          maxlength="100"
        /></label
      >
      <label>Reason <input name="reason" required maxlength="500" /></label>
      <button>Prepare seven-day invitation</button>
    </form>
    <h2>Invitations</h2>
    {#each data.invitations.items as invitation (invitation.id)}
      <article>
        <p>
          {invitation.recipient_label}: {invitation.status}. Expires {invitation.expires_at}.
        </p>
        {#if ["pending", "awaiting_delivery"].includes(invitation.status)}
          <form method="POST">
            <input type="hidden" name="invitation_id" value={invitation.id} />
            <input type="hidden" name="request_id" value={requestId()} />
            <label
              >Reason <input name="reason" required maxlength="500" /></label
            >
            <button
              name="action"
              value={invitation.status === "pending" ? "reissue" : "deliver"}
            >
              {invitation.status === "pending"
                ? "Reissue and invalidate old link"
                : "Show signup link once"}
            </button>
            <button name="action" value="revoke_invitation">Revoke</button>
          </form>
        {/if}
      </article>
    {/each}
    <h2>Users</h2>
    {#each data.users.items as user (user.id)}
      <article>
        <h3>{user.username}</h3>
        <p>
          {user.id}: {user.active ? "Active" : "Disabled"}. Grants: {user.permissions.join(
            ", ",
          ) || "None"}.
        </p>
        <form method="POST">
          <input type="hidden" name="user_id" value={user.id} />
          <input type="hidden" name="request_id" value={requestId()} />
          <input
            type="hidden"
            name="active"
            value={user.active ? "false" : "true"}
          />
          <label>Reason <input name="reason" required maxlength="500" /></label>
          <button name="action" value="set_active"
            >{user.active
              ? "Disable platform access"
              : "Enable platform account"}</button
          >
          <label
            >Permission <select name="permission">
              {#each data.permissions as permission (permission.name)}<option
                  value={permission.name}>{permission.name}</option
                >{/each}
            </select></label
          >
          <button name="action" value="grant">Grant</button>
          <button name="action" value="revoke_grant">Revoke grant</button>
        </form>
      </article>
    {/each}
    {#if data.users.next || data.invitations.next}<p>
        This page shows the first 50 entries. Use the paginated management API
        or MCP for more.
      </p>{/if}
  {/if}
  <a href="/grimoire">Back to Grimoire</a>
</main>

<style>
  main {
    max-width: 860px;
    margin: auto;
    padding: 2rem 1rem;
  }
  article {
    border: 1px solid currentColor;
    padding: 1rem;
    margin: 1rem 0;
  }
  form,
  label {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin: 0.75rem 0;
  }
  input {
    min-width: 240px;
  }
</style>
