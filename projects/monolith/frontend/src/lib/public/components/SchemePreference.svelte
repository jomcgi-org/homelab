<script module>
  const preferenceContext = Symbol("scheme-preference");
</script>

<script>
  import { hasContext, setContext } from "svelte";

  let { children } = $props();
  const inherited = hasContext(preferenceContext);
  setContext(preferenceContext, true);
</script>

<!-- Shared before-paint preference bootstrap. Documents without a visible
     scheme button use the same persisted choice, with CSS resolving the OS
     fallback. Wrapping layout children gives nested toggles the same owner. -->
<svelte:head>
  {#if !inherited}
    <script>
      try {
        var t = localStorage.getItem("td-theme");
        if (t === "light" || t === "dark")
          document.documentElement.dataset.theme = t;
      } catch (e) {}
    </script>
  {/if}
</svelte:head>

{@render children?.()}
