<script>
  import Sheets from "../../../private/grimoire/+page.svelte";
  let { data, form } = $props();
</script>

<a href="/grimoire">← Your campaigns</a>
<Sheets {data} {form} />
