import { Diagram, DBox, DArrow } from "./parts";

export default function Ships() {
  return (
    <Diagram label="AIS pipeline">
      <DBox role="external">AISStream.io</DBox>
      <DArrow label="WebSocket" />
      <DBox role="process" sub="in monolith">
        AIS ingest
      </DBox>
      <DArrow label="batched write" />
      <DBox role="store" sub="daily partitions">
        Postgres
      </DBox>
      <DArrow label="snapshot" />
      <DBox role="process" sub="SSR">
        ships-api
      </DBox>
      <DArrow />
      <DBox role="external">Cloudflare CDN</DBox>
      <DArrow />
      <DBox role="output" sub="MapLibre">
        jomcgi.dev/app/ships
      </DBox>
    </Diagram>
  );
}
