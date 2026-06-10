<script>
  /**
   * Rotated label with flat shadow — the "sticker" motif.
   * @type {{ color?: string, rotate?: number, class?: string, children: import('svelte').Snippet }}
   */
  let {
    color = "var(--coral)",
    rotate = -4,
    class: className = "",
    children,
  } = $props();
</script>

<span
  class="sticker {className}"
  style:background-color={color}
  style:transform="rotate({rotate}deg)"
>
  {@render children()}
</span>

<style>
  .sticker {
    display: inline-block;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    padding: 8px 14px;
    border: 2px solid var(--ink);
    /* The fill must not paint under the border: on a rotated element the
       border's outer antialiasing blends with the fill, leaving a colored
       hairline between border and shadow. drop-shadow (vs box-shadow) traces
       the rotated silhouette exactly, so the shadow meets the border with no
       seam or stepped corner. */
    background-clip: padding-box;
    filter: drop-shadow(3px 3px 0 var(--ink));
    white-space: nowrap;
    color: var(--ink);
  }
</style>
