<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Agent platform">
  <DGroup label="Triggers" stack>
    <DBox role="source">Routines</DBox>
    <DBox role="source">Discord</DBox>
    <DBox role="source">Alerts</DBox>
  </DGroup>
  <DArrow label="jobs" />
  <DBox role="process" sub="Go + JetStream">Orchestrator</DBox>
  <DArrow label="dispatch" />
  <DGroup label="Sandbox pod" stack>
    <DBox role="process" sub="Claude / Goose">Agent</DBox>
    <DBox role="output" sub="isolated">Workspace</DBox>
  </DGroup>
  <DArrow label="tool calls" />
  <DBox role="store" sub="MCP gateway, RBAC">Context Forge</DBox>
  <DArrow />
  <DGroup label="Tools" stack>
    <DBox role="output">Cluster</DBox>
    <DBox role="output">Knowledge graph</DBox>
    <DBox role="output" sub="vLLM on 4090">Inference</DBox>
  </DGroup>
</Diagram>
