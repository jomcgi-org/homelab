<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="AIS pipeline">
  <DBox role="external">AISStream.io</DBox>
  <DArrow label="WebSocket" />
  <DBox role="process">ais-ingest</DBox>
  <DArrow />
  <DBox role="store">NATS JetStream</DBox>
  <DArrow label="replay + subscribe" />
  <DBox role="process">ships-api</DBox>
  <DArrow />
  <DBox role="output" sub="MapLibre">ships.jomcgi.dev</DBox>
</Diagram>
