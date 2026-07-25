<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
  import {
    goosecrackerDispatchMs,
    goosecrackerRootfsMs,
    goosecrackerBootMs,
    goosecrackerGuestInitMs,
    goosecrackerAgentUpMs,
    agentFirstModelCallMs,
  } from "$lib/public/fcstory/metrics.js";
</script>

<Diagram label="Goosecracker: prompt to artifact">
  <DBox role="source" sub="owner only">Discord /goosecracker</DBox>
  <DArrow label="{goosecrackerDispatchMs}ms dispatch" />
  <DGroup
    label="Firecracker microVM ({goosecrackerRootfsMs +
      goosecrackerBootMs +
      goosecrackerGuestInitMs}ms cold start)"
    stack
  >
    <DBox role="process" sub="CoW rootfs {goosecrackerRootfsMs}ms">Boot</DBox>
    <DBox role="process" sub="FC restore {goosecrackerBootMs}ms">microVM</DBox>
    <DBox role="process" sub="init ~{goosecrackerGuestInitMs}ms"
      >Guest PID 1</DBox
    >
  </DGroup>
  <DArrow label="goose init {goosecrackerAgentUpMs}ms" />
  <DBox role="process" sub="~{agentFirstModelCallMs}ms to first RPC">Agent</DBox
  >
  <DArrow label="builds + publishes" />
  <DGroup label="Artifact" stack>
    <DBox role="output" sub="self-contained HTML">Artifact</DBox>
    <DBox role="output" sub="strict CSP, hot reload">jomcgi.dev/artifact</DBox>
  </DGroup>
</Diagram>
