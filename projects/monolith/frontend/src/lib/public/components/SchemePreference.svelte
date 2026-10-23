<!-- Shared before-paint preference bootstrap. Documents without a visible
     scheme button use the same persisted choice, with CSS resolving the OS
     fallback. There is no second listener or page-owned day/night state. -->
<svelte:head>
  <script>
    try {
      var t = localStorage.getItem("td-theme");
      if (t === "light" || t === "dark")
        document.documentElement.dataset.theme = t;
    } catch (e) {}
  </script>
</svelte:head>
