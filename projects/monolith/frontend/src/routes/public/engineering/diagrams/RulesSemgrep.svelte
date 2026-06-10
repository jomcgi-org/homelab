<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Hermetic scanning">
  <DBox role="source">PyPI wheels</DBox>
  <DArrow label="extract" />
  <DBox role="process" sub="OCaml binary">semgrep-core</DBox>
  <DArrow />
  <DBox role="store" sub="digest-pinned">GHCR</DBox>
  <DArrow />
  <DBox role="process" sub="3 rule types">Bazel tests</DBox>
  <DArrow />
  <DBox role="output" sub="30s cached">Pass / Fail</DBox>
</Diagram>
