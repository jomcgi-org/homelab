import { Diagram, DGroup, DBox, DArrow } from "./parts";

export default function Sextant() {
  return (
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
      <DBox role="output">CI regen check</DBox>
    </Diagram>
  );
}
