<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Model cache">
  <DBox role="source">Pod create</DBox>
  <DArrow />
  <DBox role="process" sub="admission webhook">PodMutator</DBox>
  <DArrow label="rewrite + gate" />
  <DBox role="store">ModelCache CR</DBox>
  <DArrow />
  <DBox role="process" sub="hf2oci">Sync job</DBox>
  <DArrow />
  <DGroup label="External" stack>
    <DBox role="external">HuggingFace</DBox>
    <DBox role="store">OCI registry</DBox>
  </DGroup>
  <DArrow label="ready" />
  <DBox role="output">Pod ungated</DBox>
</Diagram>
