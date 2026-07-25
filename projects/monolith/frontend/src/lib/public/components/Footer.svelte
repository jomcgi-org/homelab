<script>
  import Sticker from "./Sticker.svelte";
</script>

<footer class="footer">
  <!-- decorative shapes -->
  <svg class="deco deco-star" viewBox="0 0 40 40">
    <path
      d="M20,2 L22.5,14 L34,10 L26,20 L34,30 L22.5,26 L20,38 L17.5,26 L6,30 L14,20 L6,10 L17.5,14 Z"
      fill="var(--paper)"
      stroke="var(--ink)"
      stroke-width="2"
      stroke-linejoin="round"
    />
  </svg>
  <svg class="deco deco-cloud" viewBox="0 0 120 60">
    <path
      d="M10,45 Q 8,30 22,28 Q 26,14 42,18 Q 54,10 66,20 Q 82,16 88,30 Q 104,28 108,44 Q 108,52 98,52 L 20,52 Q 10,52 10,45 Z"
      fill="none"
      stroke="var(--ink)"
      stroke-width="2.5"
      stroke-linejoin="round"
    />
  </svg>

  <div class="wrap footer-content">
    <Sticker color="var(--coral)" rotate={-4} class="footer-sticker"
      >GET IN TOUCH</Sticker
    >
    <h2 class="footer-headline">LET'S<br />BUILD SOMETHING.</h2>
    <div class="footer-links">
      <a href="mailto:joe@jomcgi.dev" class="btn btn-primary">JOE@JOMCGI.DEV</a>
      <a
        href="https://github.com/jomcgi"
        target="_blank"
        rel="noreferrer"
        class="btn btn-secondary">GITHUB</a
      >
      <a
        href="https://linkedin.com/in/jomcgi"
        target="_blank"
        rel="noreferrer"
        class="btn btn-secondary">LINKEDIN</a
      >
    </div>
  </div>
</footer>

<style>
  .footer {
    background: var(--paper);
    border-top: 2px solid var(--ink);
    padding: 80px 0 60px;
    position: relative;
    overflow: hidden;
  }

  .footer-content {
    max-width: 900px;
    text-align: center;
    display: flex;
    flex-direction: column;
    align-items: center;
  }

  .footer-headline {
    font-family: var(--mono);
    font-weight: 700;
    font-size: clamp(32px, 4.5vw, 64px);
    line-height: 1.05;
    letter-spacing: -0.005em;
    text-transform: uppercase;
    margin: 0 0 28px;
  }

  .footer-links {
    display: flex;
    gap: 14px;
    justify-content: center;
    flex-wrap: wrap;
    margin-bottom: 48px;
  }

  /* Off-axis on purpose: the sticker hangs left of the centered headline. */
  :global(.footer-sticker) {
    display: inline-block;
    font-size: 14px;
    margin-bottom: 24px;
    align-self: flex-start;
  }

  .deco {
    position: absolute;
    pointer-events: none;
  }

  .deco-star {
    width: 80px;
    height: 80px;
    top: 30px;
    right: 10%;
    transform: rotate(15deg);
  }

  .deco-cloud {
    width: 120px;
    height: 60px;
    bottom: 20px;
    right: 5%;
    opacity: 0.6;
  }

  @media (max-width: 768px) {
    :global(.footer-sticker) {
      align-self: center;
    }
    .footer {
      text-align: center;
    }
    .footer-content {
      padding-top: 16px;
    }
    .deco {
      display: none;
    }
    .deco-star {
      display: block;
      width: 40px;
      height: 40px;
      top: 16px;
      right: 16px;
    }
  }
</style>
