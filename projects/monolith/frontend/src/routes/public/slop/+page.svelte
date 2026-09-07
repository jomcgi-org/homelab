<script>
  import { Seo } from "$lib/public/components";
  import Trail from "./Trail.svelte";
</script>

<Seo
  title="Slop · jomcgi.dev"
  description="Draft pages. Not listed, wording in flux."
  path="/slop"
/>

<main class="td slop-page">
  <div class="frame">
    <h1 class="sr-only">Slop</h1>

    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">
          <Trail />
        </div>
      </aside>

      <div class="content">
        <section class="intro">
          <p class="sec-label">/ Drafts</p>
          <h2>Pages here are drafts.</h2>
          <p>
            They are hosted but not listed, and their wording is still moving.
          </p>
        </section>
      </div>
    </div>
  </div>
</main>

<style>
  .slop-page {
    box-sizing: border-box;
    min-height: 100vh;
    padding: 1.5rem clamp(4rem, 5vw, 4.5em) 6em;
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 1rem;
  }

  .slop-page a:focus-visible,
  .slop-page summary:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }

  .frame {
    position: relative;
    max-width: 75em;
    margin: 0 auto;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .trail-dock {
    margin-bottom: 1.4rem;
  }

  .sec-label {
    margin: 0 0 1em;
    padding-bottom: 0.55em;
    border-bottom: 1px solid var(--stroke);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68rem;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .journal {
    display: grid;
    grid-template-columns: minmax(12em, 15em) minmax(0, 54em);
    gap: clamp(2em, 4vw, 4em);
  }

  .spine {
    position: sticky;
    top: 1.5rem;
    align-self: start;
    max-height: calc(100vh - 3rem);
    padding-left: 0.4rem;
    overflow-y: auto;
    scrollbar-width: none;
  }

  .intro {
    padding: 1rem;
    border: 1px solid var(--ink);
  }

  .intro h2 {
    margin: 1rem 0 0;
    font-size: 1.2rem;
    font-weight: 700;
    letter-spacing: -0.02em;
  }

  .intro > p:last-child {
    margin: 0.65rem 0 0;
    color: var(--ink);
    font-size: 0.92rem;
    line-height: 1.55;
  }

  @media (max-width: 760px) {
    .slop-page {
      padding: 1rem 1em 4em;
    }

    .journal {
      display: block;
    }

    .spine {
      position: static;
      max-height: none;
      padding-left: 0;
      overflow: visible;
    }

    .trail-dock {
      display: none;
    }
  }
</style>
