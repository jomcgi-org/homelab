<script>
  import { ChartFrame, Legend } from "@homelab/design-system/data-display";
  let props = $props();
</script>

<ChartFrame
  title={"title" in props ? props.title : "Synthetic chart"}
  units={"units" in props ? props.units : "bytes"}
  description={"description" in props
    ? props.description
    : "Exact synthetic measurements"}
  state={props.state ?? "ready"}
  message="Synthetic explanation"
>
  <Legend />
  {#snippet fallback(current)}
    <table>
      <caption>Synthetic measurements: {current}</caption>
      <thead
        ><tr><th scope="col">Series</th><th scope="col">Bytes</th></tr></thead
      >
      <tbody><tr><th scope="row">GPU</th><td>0</td></tr></tbody>
    </table>
  {/snippet}
</ChartFrame>
