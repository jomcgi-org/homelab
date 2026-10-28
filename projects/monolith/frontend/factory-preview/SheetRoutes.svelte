<script>
  import Root from "@production/routes/+layout.svelte";
  import Public from "@production/routes/public/+layout.svelte";
  import Private from "@production/routes/private/+layout.svelte";
  import BlogLayout from "@production/routes/public/blog/+layout.svelte";
  import SlopLayout from "@production/routes/public/slop/+layout.svelte";
  import Blog from "@production/routes/public/blog/+page.svelte";
  import Post from "@production/routes/public/blog/[slug]/+page.svelte";
  import Updates from "@production/routes/private/updates/+page.svelte";
  import Overview from "@production/routes/public/slop/factory/+page.svelte";
  import Activity from "@production/routes/public/slop/factory/activity/+page.svelte";
  import Context from "@production/routes/public/slop/factory/context/+page.svelte";
  import Task from "@production/routes/public/slop/factory/activity/[issue]/+page.svelte";
  import Attempt from "@production/routes/public/slop/factory/activity/[issue]/[node]/[attempt]/+page.svelte";
  import WorkItem from "@production/routes/public/slop/factory/work-items/[id]/+page.svelte";
  import {
    banner as fixtureBanner,
    data as fixtureData,
    view as fixtureView,
  } from "./sheet-state.svelte.js";
  let {
    banner = fixtureBanner,
    data = fixtureData,
    view = fixtureView,
  } = $props();
  let mounted = $state(true);
  window.__fixtureUnmount = () => (mounted = false);
</script>

{#if mounted}
  <Root>
    {#if view.startsWith("updates")}
      <Private><Updates {data} /></Private>
    {:else}
      <Public data={{ maintenanceBanner: banner }}>
        {#if ["blog", "blog-empty", "post", "single", "landing"].includes(view)}
          <BlogLayout>
            {#if view.startsWith("blog")}<Blog {data} />{:else}<Post
                {data}
              />{/if}
          </BlogLayout>
        {:else}
          <SlopLayout>
            {#if view === "overview"}<Overview {data} />
            {:else if view === "activity"}<Activity {data} />
            {:else if view === "context" || view === "chapter" || view === "search"}<Context
                {data}
              />
            {:else if view === "task"}<Task {data} />
            {:else if view === "attempt"}<Attempt {data} />
            {:else if view === "work-item"}<WorkItem {data} />{/if}
          </SlopLayout>
        {/if}
      </Public>
    {/if}
  </Root>
{/if}
