<!-- Shared before-paint preference bootstrap. Documents without a visible
     scheme button use the same persisted choice, with CSS resolving the OS
     fallback. There is no second listener or page-owned day/night state. -->
<svelte:head>
  <script>
    try {
      var t = localStorage.getItem("td-theme");
      if (t === "light" || t === "dark") {
        document.documentElement.dataset.theme = t;
        // The theme-color metas in app.html follow the OS until a stored
        // choice overrides them. Same sheet values as technical-drawing.css;
        // this runs before any stylesheet, so they cannot be read from it.
        var metas = document.querySelectorAll('meta[name="theme-color"]');
        for (var i = 0; i < metas.length; i++)
          metas[i].setAttribute(
            "content",
            t === "dark" ? "#181a20" : "#ffffff",
          );
      }
    } catch (e) {}
  </script>
</svelte:head>
