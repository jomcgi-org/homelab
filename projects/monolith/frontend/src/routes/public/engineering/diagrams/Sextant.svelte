<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Code generation">
  <DBox role="source">YAML spec</DBox>
  <DArrow />
  <DBox role="process">Parse + validate</DBox>
  <DArrow />
  <DBox role="process">Generator</DBox>
  <DArrow />
  <DGroup label="Generated Go" stack>
    <DBox role="output">types.go</DBox>
    <DBox role="output">transitions.go</DBox>
    <DBox role="output">metrics.go</DBox>
    <DBox role="output">status.go</DBox>
  </DGroup>
  <DArrow label="drift guard" />
  <DBox role="external">CI regen check</DBox>
</Diagram>
