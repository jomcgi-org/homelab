<script>
  import AgentScrollStory from "$lib/public/agentstory/AgentScrollStory.svelte";
</script>

<svelte:head>
  <title>Every agent gets its own machine · ember/agents</title>
  <meta
    name="description"
    content="How this site runs its AI agents: each session in its own Firecracker microVM, restored from a shared base snapshot in 2.5 ms, credentials held outside the VM, workspaces tiered to S3."
  />
  <noscript>
    <style>
      .scroller {
        display: none !important;
      }
      .static-story {
        display: block !important;
      }
    </style>
  </noscript>
</svelte:head>

<AgentScrollStory />
