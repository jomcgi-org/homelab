<script>
  import "$lib/grimoire/theme.css";
  let { data, form } = $props();
</script>

<svelte:head><title>Your campaigns · Grimoire</title></svelte:head>

<main class="grimoire lobby">
  <header>
    <p class="eyebrow">Grimoire</p>
    <h1>Your next adventure starts here.</h1>
    <p>Welcome, {data.user.display_name || data.user.email}.</p>
    <nav aria-label="Account">
      <a href="/grimoire/sheets">Character sheets</a>
      {#if data.can_administer_accounts}
        <a href="https://auth.jomcgi.dev/if/admin/#/flow/stages/invitations"
          >Account invitations ↗</a
        >
      {/if}
      <a href="/grimoire/oauth2/logout">Sign out</a>
    </nav>
  </header>

  {#if form?.error}<p class="notice" role="alert">{form.error}</p>
  {:else if form?.ok}<p class="notice" role="status">Saved.</p>{/if}

  {#if data.can_administer_accounts}
    <aside>
      <strong>Invite a friend to Grimoire</strong>
      <p>
        Open Account invitations, choose the Grimoire enrollment flow, and
        create a single-use invitation with their email in Fixed data. Share the
        generated link with your friend. Once they sign in to Grimoire, campaign
        owners can invite them to a table.
      </p>
      <code>{'{"email": "friend@example.com"}'}</code>
    </aside>
  {/if}

  {#if data.invitations.length}
    <section aria-labelledby="invites-heading">
      <h2 id="invites-heading">Your invitations</h2>
      {#each data.invitations as invite (invite.id)}
        <article>
          <h3>{invite.campaign_name}</h3>
          <p>Join this campaign as a player.</p>
          <form method="POST" action="?/accept">
            <input type="hidden" name="invitation_id" value={invite.id} />
            <button>Accept invitation</button>
            <button formaction="?/decline" class="secondary">Decline</button>
          </form>
        </article>
      {/each}
    </section>
  {/if}

  <section aria-labelledby="campaigns-heading">
    <h2 id="campaigns-heading">Your campaigns</h2>
    {#if !data.campaigns.length}
      <p>
        No campaigns yet. Create your own, or ask an owner to invite <strong
          >{data.user.email}</strong
        >.
      </p>
    {/if}
    {#each data.campaigns as campaign (campaign.id)}
      <article>
        <p class="eyebrow">
          {campaign.is_owner
            ? "Owner · DM"
            : campaign.role === "dm"
              ? "DM"
              : "Player"}
        </p>
        <h3>{campaign.name}</h3>
        {#if campaign.needs_character}
          <form method="POST" action="?/createCharacter">
            <input type="hidden" name="campaign_id" value={campaign.id} />
            <label
              >Character name <input
                name="name"
                required
                maxlength="120"
              /></label
            >
            <button>Create your character</button>
          </form>
        {:else if campaign.role === "player"}
          <p>Your character: {campaign.character_name}</p>
        {/if}
        {#if data.playEnabled}<a
            href={`/grimoire/campaigns/${campaign.id}/session`}>Open session</a
          >{/if}
        <a
          href={campaign.player_character_id
            ? `/grimoire/sheets#character-${campaign.player_character_id}`
            : "/grimoire/sheets"}>Open character sheets</a
        >
        <a href={`/grimoire/campaigns/${campaign.id}/notes`}>Open notes</a>
        <a href={`/grimoire/campaigns/${campaign.id}/journal`}>Open journal</a>
        {#if campaign.role === "dm"}
          <details>
            <summary
              >{campaign.is_owner
                ? "Players and invitations"
                : "Players"}</summary
            >
            <ul>
              {#each campaign.members as member (member.id)}
                <li>
                  <span
                    >{member.email} · {member.role === "dm" ? "DM" : "Player"} · {member.character_name ||
                      "No character"}</span
                  >
                  {#if member.role === "player"}
                    <details class="assignment">
                      <summary>Assign character</summary>
                      {#if campaign.unassigned_characters.length}
                        <form method="POST" action="?/assign">
                          <input
                            type="hidden"
                            name="campaign_id"
                            value={campaign.id}
                          />
                          <input
                            type="hidden"
                            name="member_id"
                            value={member.id}
                          />
                          <input type="hidden" name="mode" value="existing" />
                          <label
                            >Unassigned character
                            <select name="player_character_id" required>
                              <option value="">Choose a character</option>
                              {#each campaign.unassigned_characters as character (character.id)}
                                <option value={character.id}
                                  >{character.character_name}</option
                                >
                              {/each}
                            </select></label
                          >
                          <button>Assign existing character</button>
                        </form>
                      {:else}
                        <p>No unassigned characters. Create one below.</p>
                      {/if}
                      <form method="POST" action="?/assign">
                        <input
                          type="hidden"
                          name="campaign_id"
                          value={campaign.id}
                        />
                        <input
                          type="hidden"
                          name="member_id"
                          value={member.id}
                        />
                        <input type="hidden" name="mode" value="new" />
                        <label
                          >New character name <input
                            name="name"
                            required
                            maxlength="120"
                          /></label
                        >
                        <button>Create and assign character</button>
                      </form>
                      {#if member.player_character_id}
                        <form method="POST" action="?/assign">
                          <input
                            type="hidden"
                            name="campaign_id"
                            value={campaign.id}
                          />
                          <input
                            type="hidden"
                            name="member_id"
                            value={member.id}
                          />
                          <input type="hidden" name="mode" value="clear" />
                          <button class="secondary"
                            >Clear character assignment</button
                          >
                        </form>
                      {/if}
                    </details>
                  {/if}
                  {#if campaign.is_owner && member.role === "player"}
                    <form method="POST" action="?/remove">
                      <input
                        type="hidden"
                        name="campaign_id"
                        value={campaign.id}
                      />
                      <input type="hidden" name="member_id" value={member.id} />
                      <button class="secondary">Remove player</button>
                    </form>
                  {/if}
                </li>
              {/each}
            </ul>
            {#if campaign.is_owner}
              <form method="POST" action="?/invite">
                <input type="hidden" name="campaign_id" value={campaign.id} />
                <label
                  >Registered player's email <input
                    type="email"
                    name="email"
                    required
                    maxlength="320"
                  /></label
                >
                <button>Invite player</button>
              </form>
              <p class="hint">
                They must have signed in to Grimoire once. They get access after
                accepting.
              </p>
              {#each campaign.invitations as invite (invite.id)}
                <form method="POST" action="?/cancel">
                  <span>{invite.invitee_email} · Pending</span>
                  <input type="hidden" name="campaign_id" value={campaign.id} />
                  <input type="hidden" name="invitation_id" value={invite.id} />
                  <button class="secondary">Cancel invitation</button>
                </form>
              {/each}
            {/if}
          </details>
        {/if}
      </article>
    {/each}
  </section>

  <section aria-labelledby="create-heading">
    <h2 id="create-heading">Start a campaign</h2>
    <p>You will be its owner and first DM.</p>
    <form method="POST" action="?/create">
      <label
        >Campaign name <input
          name="name"
          required
          maxlength="120"
          placeholder="The next adventure"
        /></label
      >
      <button>Create campaign</button>
    </form>
  </section>
</main>

<style>
  .lobby {
    max-width: 860px;
    margin: auto;
    padding: 3rem 1.25rem;
    min-height: 100vh;
  }
  header,
  section {
    margin-bottom: 2.5rem;
  }
  h1 {
    font-size: clamp(2rem, 5vw, 3.5rem);
    line-height: 1.12;
  }
  h2 {
    font-size: 1.5rem;
  }
  h3 {
    margin-top: 0;
  }
  .eyebrow {
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }
  nav,
  form,
  li {
    display: flex;
    flex-wrap: wrap;
    align-items: end;
    gap: 1rem;
  }
  nav {
    margin-top: 1.5rem;
  }
  a {
    color: inherit;
    text-underline-offset: 0.2em;
  }
  article,
  aside {
    border: 1px solid var(--grim-line);
    border-radius: 0.6rem;
    padding: 1.5rem;
    margin: 1rem 0;
  }
  label {
    display: grid;
    gap: 0.5rem;
    flex: 1;
    min-width: 180px;
  }
  input,
  select {
    padding: 0.7rem;
    font: inherit;
    border: 1px solid var(--grim-line);
    border-radius: 0.3rem;
    background: transparent;
    color: inherit;
  }
  button {
    padding: 0.75rem 1rem;
    border: 1px solid var(--grim-accent);
    border-radius: 0.3rem;
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    font: inherit;
    cursor: pointer;
  }
  button.secondary {
    background: transparent;
    color: inherit;
  }
  details {
    margin-top: 1.5rem;
  }
  .assignment {
    flex-basis: 100%;
    margin-top: 0;
  }
  .assignment form {
    margin-top: 1rem;
  }
  summary {
    cursor: pointer;
  }
  ul {
    padding: 0;
    list-style: none;
  }
  li {
    justify-content: space-between;
    margin: 1rem 0;
  }
  .hint {
    font-size: 0.9rem;
    opacity: 0.8;
  }
  .notice {
    border-left: 3px solid var(--grim-accent);
    padding: 1rem;
  }
</style>
