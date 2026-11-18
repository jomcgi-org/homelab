import { Diagram, DGroup, DBox, DArrow } from "./parts";

export default function Bazel() {
  return (
    <Diagram label="One build graph">
      <DGroup label="Anywhere" stack>
        <DBox role="source">Laptop</DBox>
        <DBox role="source">CI</DBox>
        <DBox role="source">Agents</DBox>
      </DGroup>
      <DArrow />
      <DBox role="process">format</DBox>
      <DArrow />
      <DGroup label="Targets" stack>
        <DBox role="process">Code</DBox>
        <DBox role="process">Helm manifests</DBox>
        <DBox role="process">apko images</DBox>
      </DGroup>
      <DArrow />
      <DBox role="store" sub="remote cache">
        BuildBuddy RBE
      </DBox>
      <DArrow />
      <DGroup label="Outputs" stack>
        <DBox role="output">Git</DBox>
        <DBox role="output">Registry</DBox>
      </DGroup>
    </Diagram>
  );
}
