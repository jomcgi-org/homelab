<script>
  const ICON_PATHS = {
    check:
      '<path d="M3.2 8.6l3.2 3.2 6.4-7.4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    play: '<path d="M5 3.4L12.6 8 5 12.6z" fill="currentColor"/>',
    hourglass:
      '<path d="M4.4 3h7.2M4.4 13h7.2M5 3.2c0 4 6 4 6 9.6M11 3.2c0 4-6 4-6 9.6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    ring: '<circle cx="8" cy="8" r="4.7" fill="none" stroke="currentColor" stroke-width="1.7" stroke-dasharray="2.4 2.6"/>',
    person:
      '<circle cx="8" cy="5.2" r="2.4" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M3.4 13.4c0-2.6 2-4.2 4.6-4.2s4.6 1.6 4.6 4.2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    link: '<path d="M6.7 9.3l2.6-2.6M5.2 7.3L3.9 8.6a2.5 2.5 0 003.5 3.5l1.3-1.3M10.8 8.7l1.3-1.3a2.5 2.5 0 00-3.5-3.5L7.3 5.2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    alert:
      '<path d="M8 2.6L14 13H2z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M8 6.4v3" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="8" cy="11.2" r="0.9" fill="currentColor"/>',
    cross:
      '<path d="M4.4 4.4l7.2 7.2M11.6 4.4l-7.2 7.2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    nosign:
      '<circle cx="8" cy="8" r="5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M4.6 4.6l6.8 6.8" stroke="currentColor" stroke-width="1.7"/>',
    diamond:
      '<path d="M8 2.6L13.4 8 8 13.4 2.6 8z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
    diamond_filled:
      '<path d="M8 2.6L13.4 8 8 13.4 2.6 8z" fill="currentColor"/>',
    dots: '<circle cx="3.6" cy="8" r="1.4" fill="currentColor"/><circle cx="8" cy="8" r="1.4" fill="currentColor"/><circle cx="12.4" cy="8" r="1.4" fill="currentColor"/>',
  };
  const ICON_NAMES = {
    done: "check",
    running: "play",
    queued: "hourglass",
    future: "ring",
    blocked_human: "person",
    blocked_dep: "link",
    escalated: "alert",
    failed: "cross",
    cancelled: "nosign",
    gate: "diamond",
    gate_passed: "diamond_filled",
    expansion: "dots",
  };
  let { icon = "future", class: className = "" } = $props();
</script>

<span class={`ic ${className}`} aria-hidden="true"
  ><svg viewBox="0 0 16 16"
    >{@html ICON_PATHS[ICON_NAMES[icon] || icon] || ""}</svg
  ></span
>
