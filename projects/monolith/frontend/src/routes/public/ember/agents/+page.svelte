<script>
  import AgentScrollStory from "$lib/public/agentstory/AgentScrollStory.svelte";
</script>

<svelte:head>
  <title>One microVM per agent session · ember/agents</title>
  <meta
    name="description"
    content="How this site runs its AI agents: one Firecracker microVM per session, restored from a shared base snapshot in 2.5 ms, killed 20 s after the last turn, rebuilt from a workspace tiered to S3. Credentials stay outside the VM."
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
