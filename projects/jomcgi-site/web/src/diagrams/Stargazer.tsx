import { Diagram, DGroup, DBox, DArrow } from "./parts";

export default function Stargazer() {
  return (
    <Diagram label="Forecast scoring">
      <DGroup label="Sources" stack>
        <DBox role="store" sub="from SeaweedFS">
          LP site grid
        </DBox>
        <DBox role="external">MET Norway</DBox>
      </DGroup>
      <DArrow label="hourly" />
      <DBox role="process" sub="dark + clear">
        Score hours
      </DBox>
      <DArrow />
      <DBox role="store" sub="qualifying hours">
        stars.site_hours
      </DBox>
      <DArrow />
      <DBox role="output" sub="SSR, edge-cached">
        jomcgi.dev/app/stars
      </DBox>
    </Diagram>
  );
}
