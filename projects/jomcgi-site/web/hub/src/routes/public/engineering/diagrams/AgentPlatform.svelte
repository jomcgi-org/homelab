<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
  import { agentRestoreColdMs } from "$lib/public/fcstory/metrics.js";
</script>

<Diagram label="Agent platform">
  <DGroup label="Triggers" stack>
    <DBox role="source">Routines</DBox>
    <DBox role="source">Discord</DBox>
    <DBox role="source">Alerts</DBox>
  </DGroup>
  <DArrow label="dispatch" />
  <DBox role="process" sub="Postgres reconcile">Controller</DBox>
  <DArrow label="restore {agentRestoreColdMs}ms" />
  <DGroup label="Firecracker microVM" stack>
    <DBox role="process" sub="vsock-only guest">Agent</DBox>
    <DBox role="output" sub="snapshot / restore">Clean VM per task</DBox>
  </DGroup>
  <DArrow label="egress" />
  <DBox role="store" sub="placeholder to real key">TLS egress proxy</DBox>
  <DArrow />
  <DGroup label="Models" stack>
    <DBox role="output" sub="llama.cpp on 4090">Qwen3.8 27B</DBox>
    <DBox role="external" sub="over swapped egress">Frontier API</DBox>
  </DGroup>
</Diagram>
