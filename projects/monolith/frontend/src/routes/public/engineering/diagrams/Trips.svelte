<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Camera to browser">
  <DBox role="source" sub="GPS interval">GoPro</DBox>
  <DArrow />
  <DBox role="store">SQLite queue</DBox>
  <DArrow label="Cloudflare Access" />
  <DBox role="process" sub="server-side">EXIF ingest</DBox>
  <DArrow />
  <DGroup label="Storage" stack>
    <DBox role="store">Postgres</DBox>
    <DBox role="store">SeaweedFS</DBox>
  </DGroup>
  <DArrow />
  <DBox role="process" sub="resize on the fly">imgproxy</DBox>
  <DArrow />
  <DBox role="external">Cloudflare CDN</DBox>
  <DArrow />
  <DBox role="output" sub="SSR, read-only">/app/trips</DBox>
</Diagram>
