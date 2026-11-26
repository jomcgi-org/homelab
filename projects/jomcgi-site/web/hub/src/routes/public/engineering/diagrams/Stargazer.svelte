<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Forecast scoring">
  <DGroup label="Sources" stack>
    <DBox role="store" sub="from SeaweedFS">LP site grid</DBox>
    <DBox role="external">MET Norway</DBox>
  </DGroup>
  <DArrow label="hourly" />
  <DBox role="process" sub="dark + clear">Score hours</DBox>
  <DArrow />
  <DBox role="store" sub="qualifying hours">stars.site_hours</DBox>
  <DArrow />
  <DBox role="output" sub="SSR, edge-cached">jomcgi.dev/app/stars</DBox>
</Diagram>
