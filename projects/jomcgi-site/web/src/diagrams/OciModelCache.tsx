import { Diagram, DGroup, DBox, DArrow } from "./parts";

export default function OciModelCache() {
  return (
    <Diagram label="Model cache">
      <DBox role="source">Pod create</DBox>
      <DArrow />
      <DBox role="process" sub="admission webhook">
        PodMutator
      </DBox>
      <DArrow label="rewrite + gate" />
      <DBox role="store">ModelCache CR</DBox>
      <DArrow />
      <DBox role="process" sub="hf2oci">
        Sync job
      </DBox>
      <DArrow />
      <DGroup label="External" stack>
        <DBox role="external">HuggingFace</DBox>
        <DBox role="store">OCI registry</DBox>
      </DGroup>
      <DArrow label="ready" />
      <DBox role="output">Pod ungated</DBox>
    </Diagram>
  );
}
