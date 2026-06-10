<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Knowledge pipeline">
  <DGroup label="Sources" stack>
    <DBox role="source">Markdown notes</DBox>
    <DBox role="source">Chat + captures</DBox>
  </DGroup>
  <DArrow label="decompose" />
  <DBox role="process" sub="Qwen, self-critique">LLM extraction</DBox>
  <DArrow />
  <DGroup label="Postgres" stack>
    <DBox role="store">Facts + edges</DBox>
    <DBox role="store">pgvector HNSW</DBox>
  </DGroup>
  <DArrow label="serve" />
  <DGroup label="Consumers" stack>
    <DBox role="output">MCP tools</DBox>
    <DBox role="output">Cmd+K search</DBox>
    <DBox role="output">Agents</DBox>
  </DGroup>
</Diagram>
