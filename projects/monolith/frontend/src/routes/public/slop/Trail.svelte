<script>
  // Slop has its own section URL, unlike the blog trail. Keeping it separate
  // avoids adding slop-only structure to the blog.
  //
  // `page` names one leaf under /slop. `crumbs` is the deeper form: an array of
  // {label, href?} rendered in order after slop, the last one current. The two
  // are alternatives, and `page` stays because most callers only need a leaf.
  import Trail from "$lib/public/components/Trail.svelte";

  let { page = "", crumbs = null } = $props();

  const trail = $derived(
    crumbs?.length ? crumbs : page ? [{ label: page }] : [],
  );
</script>

<Trail crumbs={[{ label: "slop", href: "/slop" }, ...trail]} />
