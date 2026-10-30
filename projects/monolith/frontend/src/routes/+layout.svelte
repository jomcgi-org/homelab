<script>
  import "@homelab/design-system/tokens/contract.css";
  import "$lib/global.css";

  let { children } = $props();

  // No site-wide chrome: every tier renders its own. Public pages carry the
  // technical-drawing trail and scheme switch, the private dashboard is the
  // private tier's nav, and the friends screens are their own tools. The
  // brutalist nav bar retired with its last consumer (#7160).
</script>

{@render children()}
