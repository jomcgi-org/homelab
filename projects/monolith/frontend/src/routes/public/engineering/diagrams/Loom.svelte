<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Loom (pre-alpha)">
  <DGroup label="Services" stack>
    <DBox role="source">Ingest</DBox>
    <DBox role="process">Transform</DBox>
    <DBox role="output">Query API</DBox>
  </DGroup>
  <DArrow label="embed" />
  <DBox role="process" sub="Rust">DataFusion</DBox>
  <DArrow />
  <DBox role="store" sub="S3 object storage">DuckLake tables</DBox>
  <DBox role="store" sub="queue, catalog, ontology, ACL, lineage"
    >Postgres control plane</DBox
  >
  <DArrow label="speaks" />
  <DBox role="output" sub="DuckDB-compatible">Quack protocol</DBox>
</Diagram>
