<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Zero Trust ingress">
  <DBox role="source" sub="annotations">Deployment</DBox>
  <DArrow label="watch" />
  <DBox role="process" sub="Sextant FSM">Operator</DBox>
  <DArrow />
  <DGroup label="Provisioned" stack>
    <DBox role="output">DNS record</DBox>
    <DBox role="output">Zero Trust app</DBox>
    <DBox role="output">Tunnel config</DBox>
  </DGroup>
  <DArrow />
  <DBox role="external">Cloudflare</DBox>
</Diagram>
