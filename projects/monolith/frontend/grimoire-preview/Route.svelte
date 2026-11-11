<script>
  import Root from "../src/routes/+layout.svelte";
  import Public from "../src/routes/public/+layout.svelte";
  import "../src/lib/public/styles/design-system.css";
  import Layout from "../src/routes/public/app/grimoire/+layout.svelte";
  import Home from "../src/routes/public/app/grimoire/+page.svelte";
  import Library from "../src/routes/public/app/grimoire/library/+page.svelte";
  import World from "../src/routes/public/app/grimoire/world/+page.svelte";
  import Entity from "../src/routes/public/app/grimoire/entity/[id]/+page.svelte";
  import Adventure from "../src/routes/public/app/grimoire/adventure/[id]/+page.svelte";
  import Book from "../src/routes/public/app/grimoire/book/[book]/+page.svelte";
  import Chat from "../src/routes/public/app/grimoire/chat/+page.svelte";
  import Share from "../src/routes/public/app/grimoire/chat/s/[id]/+page@.svelte";
  import Engineering from "../src/routes/public/engineering/+page.svelte";
  let { surface = "home", data } = $props();
</script>

<Root>
  {#if surface === "share"}
    <Share {data} />
  {:else}
    <Public {data}>
      {#if surface === "engineering"}<Engineering />
      {:else}
        <Layout {data}>
          {#if surface === "home"}<Home />
          {:else if surface === "library"}<Library />
          {:else if surface === "world"}<World />
          {:else if surface === "entity"}<Entity />
          {:else if surface === "adventure"}<Adventure />
          {:else if surface === "book"}<Book {data} />
          {:else if surface === "chat"}<Chat {data} />{/if}
        </Layout>
      {/if}
    </Public>
  {/if}
</Root>
