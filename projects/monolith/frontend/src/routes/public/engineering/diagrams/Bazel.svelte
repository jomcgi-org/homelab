<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="One build graph">
  <DGroup label="Anywhere" stack>
    <DBox role="source">Laptop</DBox>
    <DBox role="source">CI</DBox>
    <DBox role="source">Agents</DBox>
  </DGroup>
  <DArrow />
  <DBox role="process">format</DBox>
  <DArrow />
  <DGroup label="Targets" stack>
    <DBox role="process">Code</DBox>
    <DBox role="process">Helm manifests</DBox>
    <DBox role="process">apko images</DBox>
  </DGroup>
  <DArrow />
  <DBox role="store" sub="remote cache">BuildBuddy RBE</DBox>
  <DArrow />
  <DGroup label="Outputs" stack>
    <DBox role="output">Git</DBox>
    <DBox role="output">Registry</DBox>
  </DGroup>
</Diagram>
