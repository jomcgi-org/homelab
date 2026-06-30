<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Goosecracker: prompt to artifact">
  <DBox role="source" sub="owner only">Discord /goosecracker</DBox>
  <DArrow label="10ms dispatch" />
  <DGroup label="Firecracker microVM (84ms cold start)" stack>
    <DBox role="process" sub="CoW rootfs 35ms">Boot</DBox>
    <DBox role="process" sub="FC restore 28ms">microVM</DBox>
    <DBox role="process" sub="init ~20ms">Guest PID 1</DBox>
  </DGroup>
  <DArrow label="goose init 50ms" />
  <DBox role="process" sub="~140ms to first RPC">Agent</DBox>
  <DArrow label="builds + publishes" />
  <DGroup label="Artifact" stack>
    <DBox role="output" sub="self-contained HTML">Artifact</DBox>
    <DBox role="output" sub="strict CSP, hot reload">jomcgi.dev/artifact</DBox>
  </DGroup>
</Diagram>
