<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Camera to browser">
  <DBox role="source" sub="27MP interval">GoPro</DBox>
  <DArrow />
  <DBox role="store">SQLite queue</DBox>
  <DArrow />
  <DBox role="process">EXIF + GPS</DBox>
  <DArrow />
  <DGroup label="Storage" stack>
    <DBox role="store">SeaweedFS</DBox>
    <DBox role="store">NATS JetStream</DBox>
  </DGroup>
  <DArrow />
  <DBox role="process" sub="resize on the fly">imgproxy</DBox>
  <DArrow />
  <DBox role="external">Cloudflare CDN</DBox>
  <DArrow />
  <DBox role="output" sub="live WebSocket">trips.jomcgi.dev</DBox>
</Diagram>
