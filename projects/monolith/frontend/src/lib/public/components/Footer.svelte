<script>
  import { page } from "$app/state";

  const sections = $derived([
    { label: "Home", href: "/" },
    { label: "CV", href: "/cv" },
    { label: "Engineering", href: "/engineering" },
    ...(page.data?.hasBlog ? [{ label: "Blog", href: "/blog" }] : []),
    { label: "Docs", href: "/docs" },
  ]);
</script>

<footer class="edition" aria-labelledby="contact-heading">
  <div class="ed-head">Get in touch</div>
  <div class="ed-lead">
    <h2 id="contact-heading">LET'S BUILD SOMETHING.</h2>
  </div>
  <div class="ed-foot">
    <a href="mailto:joe@jomcgi.dev">JOE@JOMCGI.DEV</a>
    <a href="https://github.com/jomcgi" target="_blank" rel="noreferrer"
      >GITHUB</a
    >
    <a href="https://linkedin.com/in/jomcgi" target="_blank" rel="noreferrer"
      >LINKEDIN</a
    >
  </div>
  <!-- These destinations remain reachable on phones, where the spine folds. -->
  <nav class="ed-foot" aria-label="Continue reading">
    {#each sections as section (section.href)}
      <a href={section.href}>{section.label}</a>
    {/each}
  </nav>
</footer>
