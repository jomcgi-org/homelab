<script>
  import AgentScrollStory from "$lib/public/agentstory/AgentScrollStory.svelte";
  import { sessionRestoreMs } from "$lib/public/fcstory/metrics.js";
</script>

<svelte:head>
  <title>One microVM per agent session · ember/agents</title>
  <meta
    name="description"
    content="How this site runs its AI agents: one Firecracker microVM per session, restored from a shared base snapshot in {sessionRestoreMs} ms, killed 20 s after the last turn, rebuilt from a workspace tiered to S3. Credentials stay outside the VM."
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
