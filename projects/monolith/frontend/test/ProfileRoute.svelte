<script>
  import RootLayout from "../src/routes/+layout.svelte";
  import PublicLayout from "../src/routes/public/+layout.svelte";
  import Home from "../src/routes/public/+page.svelte";
  import Cv from "../src/routes/public/cv/+page.svelte";
  import Engineering from "../src/routes/public/engineering/+page.svelte";
  import Ember from "../src/routes/public/ember/+page.svelte";
  import Ships from "../src/routes/public/app/ships/+page.svelte";
  // Mirrors Kit's error-node preload, including the implicit legacy CSS.
  import RootError from "../src/routes/+error.svelte";
  let { surface = "home", data = { stats: null } } = $props();
</script>

<RootLayout>
  {#if surface === "error"}
    <RootError />
  {:else if surface === "isolated"}
    <main class="legacy-fixture">
      <a class="btn" href="/">Legacy control</a>
    </main>
  {:else}
    <PublicLayout {data}>
      {#if surface === "home"}<Home {data} />
      {:else if surface === "cv"}<Cv />
      {:else if surface === "engineering"}<Engineering />
      {:else if surface === "ships"}<Ships {data} />
      {:else if surface === "ember"}<Ember
          data={{ status: null, savings: null }}
        />
      {:else}<main class="legacy-fixture">
          <a class="btn" href="/">Legacy control</a>
        </main>{/if}
    </PublicLayout>
  {/if}
</RootLayout>
