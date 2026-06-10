<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Scoring pipeline">
  <DGroup label="Acquire" stack>
    <DBox role="source">Light pollution</DBox>
    <DBox role="source">OSM roads</DBox>
    <DBox role="source">SRTM elevation</DBox>
    <DBox role="source">MET Norway</DBox>
  </DGroup>
  <DArrow />
  <DGroup label="Process" stack>
    <DBox role="process">Dark regions</DBox>
    <DBox role="process">Road buffers</DBox>
    <DBox role="process">Zones</DBox>
  </DGroup>
  <DArrow />
  <DBox role="output" sub="0 to 100">Composite score</DBox>
</Diagram>
