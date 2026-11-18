import { Diagram, DGroup, DBox, DArrow } from "./parts";

export default function Loom() {
  return (
    <Diagram label="Loom (pre-alpha)">
      <DGroup label="Services" stack>
        <DBox role="source">Ingest</DBox>
        <DBox role="process">Transform</DBox>
        <DBox role="output">Query API</DBox>
      </DGroup>
      <DArrow label="embed" />
      <DBox role="process" sub="Rust">
        DataFusion
      </DBox>
      <DArrow />
      <DBox role="store" sub="S3 object storage">
        DuckLake tables
      </DBox>
      <DBox role="store" sub="queue, catalog, ontology, ACL, lineage">
        Postgres control plane
      </DBox>
      <DArrow label="speaks" />
      <DBox role="output" sub="DuckDB-compatible">
        Quack protocol
      </DBox>
    </Diagram>
  );
}
