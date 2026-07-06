<script>
  // Sun/moon control for the Grimoire's light/dark theme (2026-07-05 reskin).
  // Toggles document.body.classList("dark"), the switch the shared tokens
  // (lib/styles/shared/tokens.css) and this app's --grim-* tokens (theme.css)
  // both already key off; nothing else in the frontend currently drives that
  // class, so this is the first control that flips it. Persists per-device to
  // localStorage and falls back to the OS preference on first visit.
  // ssr = false everywhere this mounts, but the guard keeps the component
  // reusable in an ssr context without throwing on `document`/`localStorage`.
  import { onMount } from "svelte";

  const LS_KEY = "grimoire-theme";

  let isDark = $state(false);

  function readStoredTheme() {
    try {
      return localStorage.getItem(LS_KEY);
    } catch {
      return null;
    }
  }

  function writeStoredTheme(value) {
    try {
      localStorage.setItem(LS_KEY, value);
    } catch {
      // ignore (private mode / storage disabled)
    }
  }

  function apply(dark) {
    document.body.classList.toggle("dark", dark);
  }

  onMount(() => {
    const stored = readStoredTheme();
    const prefersDark =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches;
    isDark = stored ? stored === "dark" : prefersDark;
    apply(isDark);
  });

  function toggleTheme() {
    isDark = !isDark;
    apply(isDark);
    writeStoredTheme(isDark ? "dark" : "light");
  }
</script>

<button
  type="button"
  class="grim-theme-toggle"
  onclick={toggleTheme}
  aria-pressed={isDark}
  aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
>
  <span class="knob">
    {#if isDark}
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"
        ><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg
      >
    {:else}
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        aria-hidden="true"
        ><circle cx="12" cy="12" r="4" /><path
          d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
        /></svg
      >
    {/if}
  </span>
</button>

<style>
  .grim-theme-toggle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: none;
    width: 2.25rem;
    height: 2.25rem;
    padding: 0;
    background: var(--grim-surface-2);
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    color: var(--grim-text-dim);
    cursor: pointer;
  }

  .grim-theme-toggle:hover {
    color: var(--grim-ink);
    border-color: var(--grim-accent);
  }

  .knob {
    width: 20px;
    height: 20px;
    display: grid;
    place-items: center;
    color: var(--grim-accent);
  }

  .knob svg {
    width: 14px;
    height: 14px;
    display: block;
  }
</style>
