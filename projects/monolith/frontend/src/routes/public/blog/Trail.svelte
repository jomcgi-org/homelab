<script>
  // The blog's trail: site, then the blog, then the post being read. The
  // shared Trail draws it; this keeps the section's URL in one place.
  import Trail from "$lib/public/components/Trail.svelte";

  let { post = "" } = $props();
</script>

<Trail crumbs={[{ label: "blog", href: "/blog" }]} current={post} />
