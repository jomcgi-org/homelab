<script>
  import ReviewCard from "../src/lib/private/components/ReviewCard.svelte";
  import ModeToggle from "../src/lib/private/components/ModeToggle.svelte";
  import UndoToast from "../src/lib/private/components/UndoToast.svelte";
  import { records } from "./fixtures.js";
  let tab = $state("gaps");
  let mode = $state("pending");
  let decision = $state("No synthetic decision yet.");
  let deleted = $state(false);
  function decide(action) {
    decision = `Synthetic decision: ${action}`;
    if (action === "delete") deleted = true;
  }
</script>

<main>
  <a href="../../">All fixture demos</a>
  <h1>Private review components</h1>
  <p>
    Synthetic fixture. All records are invented. Decisions stay in this page.
  </p>
  <p>
    This composition uses the private review controls. It does not run the
    private page, its loader or authentication.
  </p>
  <nav aria-label="Record type">
    <button
      onclick={() => {
        tab = "gaps";
        deleted = false;
      }}>Gaps</button
    >
    <button
      onclick={() => {
        tab = "notes";
        deleted = false;
      }}>Notes</button
    >
  </nav>
  <ModeToggle
    {mode}
    onChange={(value) => {
      mode = value;
      deleted = false;
    }}
  />
  {#if !deleted}
    <ReviewCard item={records[tab]} {tab} {mode} onDecide={decide} />
  {:else}
    <p>Invented record removed from this page.</p>
    <UndoToast
      label="Deleted synthetic record"
      onUndo={() => {
        deleted = false;
      }}
      onDismiss={() => {
        deleted = false;
      }}
    />
  {/if}
  <p role="status" data-decision>{decision}</p>
</main>

<style>
  main {
    max-width: 54rem;
    margin: auto;
    padding: 1rem;
  }
  nav {
    display: flex;
    gap: 1rem;
    margin-block: 1rem;
  }
  button {
    font: inherit;
    padding: 0.75rem;
  }
</style>
