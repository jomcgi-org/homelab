<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="EmberVM (serving requests bypass the control plane)">
  <DGroup label="Control plane" stack>
    <DBox role="process" sub="Elixir/OTP">dispatcher + sessions</DBox>
    <DBox role="store" sub="Postgres op-log">state</DBox>
    <DBox role="output" sub="xDS">endpoint publisher</DBox>
  </DGroup>
  <DArrow label="gRPC" />
  <DBox role="process" sub="Go, forked from fc-invoke">noded</DBox>
  <DArrow label="vsock / tap" />
  <DBox role="output" sub="task, session, serving">Firecracker microVMs</DBox>
  <DGroup label="Serving hit path" stack>
    <DBox role="source">edge gateway</DBox>
    <DBox role="process" sub="programmed via xDS">node Envoy</DBox>
    <DBox role="output" sub="kernel DNAT into the tap">serving VM</DBox>
  </DGroup>
</Diagram>
