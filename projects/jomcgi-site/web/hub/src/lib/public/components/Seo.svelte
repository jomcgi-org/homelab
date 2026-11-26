<script>
  // Per-page <head> for the public tier: title, description, canonical, and
  // Open Graph + Twitter cards. Canonical/OG URLs are absolute (built from
  // PUBLIC_BASE) because crawlers and link unfurlers need a fully-qualified
  // URL, not the gateway-internal /public/* path the backend actually sees.
  //
  // og:image is intentionally omitted until a real 1200x630 asset exists on a
  // verified static path; a broken image ref unfurls worse than none, and the
  // Twitter card stays `summary` (text-only) rather than summary_large_image.
  import { PUBLIC_BASE } from "$lib/public/seo.js";

  let { title, description, path = "/", type = "website" } = $props();

  const canonical = $derived(`${PUBLIC_BASE}${path}`);
</script>

<svelte:head>
  <title>{title}</title>
  <meta name="description" content={description} />
  <link rel="canonical" href={canonical} />

  <meta property="og:type" content={type} />
  <meta property="og:site_name" content="Joe McGinley" />
  <meta property="og:title" content={title} />
  <meta property="og:description" content={description} />
  <meta property="og:url" content={canonical} />

  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content={title} />
  <meta name="twitter:description" content={description} />
</svelte:head>
