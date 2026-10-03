<script>
  import { streamedGraphParts } from "./generated-answer.js";
  let { code = "", source = "", complete = false, finalSource = "" } = $props();
  let element;
  let svg = $state("");
  let palette = $state(null);
  const id = `qwen-diagram-${Math.random().toString(36).slice(2)}`;
  let serial = 0;
  let narrow = $state(false);
  let liveLine = $derived(code.trimEnd().split("\n").at(-1) ?? "");
  $effect(() => {
    if (!element) return;
    function sync() {
      const style = getComputedStyle(element);
      const color = (key) => style.getPropertyValue(key).trim();
      palette = {
        primaryColor: color("--sheet"),
        primaryTextColor: color("--ink"),
        primaryBorderColor: color("--tone-gpu"),
        lineColor: color("--tone-ram"),
        secondaryColor: color("--sheet"),
        tertiaryColor: color("--sheet"),
        fontFamily: style.fontFamily,
      };
    }
    sync();
    const size = matchMedia("(max-width: 600px)");
    const resize = () => (narrow = size.matches);
    resize();
    size.addEventListener("change", resize);
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    const media = matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", sync);
    return () => {
      observer.disconnect();
      media.removeEventListener("change", sync);
      size.removeEventListener("change", resize);
    };
  });
  $effect(() => {
    const full = finalSource || source;
    const wrapped = full.replace(/\[([^\]]+)\]/g, (_, label) => {
      const words = label.split(/\s+/);
      const lines = [];
      for (let i = 0; i < words.length; i += 2)
        lines.push(words.slice(i, i + 2).join(" "));
      return `["${lines.join("<br/>")}"]`;
    });
    const text = narrow
      ? wrapped.replace(/^flowchart LR\b/, "flowchart TB")
      : wrapped;
    const colors = palette;
    if (
      !colors ||
      !/^flowchart\s+(LR|TD|TB)\b/.test(text) ||
      !text.includes("\n")
    ) {
      svg = "";
      return;
    }
    let cancelled = false;
    const renderId = `${id}-${serial++}`;
    (async () => {
      try {
        const { default: mermaid } = await import("mermaid");
        if (cancelled) return;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: "base",
          themeVariables: colors,
          flowchart: {
            htmlLabels: false,
            curve: "basis",
            useMaxWidth: true,
            rankSpacing: 18,
            nodeSpacing: 18,
            padding: 10,
          },
        });
        const result = await mermaid.render(renderId, text);
        if (!cancelled) {
          const document = new DOMParser().parseFromString(
            result.svg,
            "text/html",
          );
          for (const node of document.querySelectorAll("g.node")) {
            node.style.opacity = "0";
            const label = node.textContent.toLowerCase();
            const tone = /failure|explosion|oxygen|rupture/.test(label)
              ? "disk"
              : /lunar|lifeboat|conserv|power/.test(label)
                ? "ram"
                : /trajectory|burn|course/.test(label)
                  ? "hot"
                  : label.includes("cpu")
                    ? "cache"
                    : label.includes("router")
                      ? "hot"
                      : label.includes("combined")
                        ? "ram"
                        : "gpu";
            for (const shape of node.querySelectorAll("rect, polygon")) {
              shape.style.setProperty(
                "fill",
                `color-mix(in srgb, var(--tone-${tone}) 12%, var(--sheet))`,
                "important",
              );
              shape.style.setProperty(
                "stroke",
                `var(--tone-${tone})`,
                "important",
              );
              shape.style.setProperty("stroke-width", "2px", "important");
              if (shape.tagName === "rect") {
                shape.setAttribute("rx", "5");
                shape.setAttribute("ry", "5");
              }
            }
          }
          for (const edge of document.querySelectorAll(".edgePaths path"))
            edge.style.opacity = "0";
          svg = document.querySelector("svg")?.outerHTML ?? "";
        }
      } catch {
        // An incomplete statement remains available as source until the next line arrives.
        document.getElementById(renderId)?.remove();
      }
    })();
    return () => {
      cancelled = true;
    };
  });
  $effect(() => {
    const rendered = svg;
    const parts = streamedGraphParts(source);
    if (!rendered || !element) return;
    for (const node of element.querySelectorAll("g.node")) {
      const id = node.id.match(/-flowchart-([A-Za-z]\w*)-\d+$/)?.[1];
      const visible = parts.nodes.has(id);
      node.style.opacity = visible ? "1" : "0";
      node.setAttribute("aria-hidden", String(!visible));
    }
    for (const edge of element.querySelectorAll(".edgePaths path")) {
      const ids = edge.id.match(/-L_([A-Za-z]\w*?)_([A-Za-z]\w*?)_\d+$/);
      edge.style.opacity =
        ids && parts.edges.has(`${ids[1]}->${ids[2]}`) ? "1" : "0";
    }
  });
</script>

<div bind:this={element} class="generated-diagram">
  <div
    class="diagram-canvas"
    role="img"
    aria-label="Diagram generated by the model"
  >
    {@html svg}
  </div>
  {#if !complete}<div class="code-stream" aria-hidden="true">
      {liveLine}
    </div>{/if}
  <details class="diagram-source">
    <summary>Diagram source</summary>
    <pre>{code}</pre>
  </details>
</div>

<style>
  .generated-diagram {
    min-width: 0;
    margin-top: 0.8rem;
  }
  .diagram-canvas {
    min-width: 0;
    min-height: 10rem;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .diagram-canvas :global(svg) {
    width: 100%;
    height: auto;
    overflow: visible;
  }
  .diagram-canvas :global(.node),
  .diagram-canvas :global(.edgePaths path) {
    transition: opacity 0.25s ease;
  }
  .diagram-canvas :global(.edgePaths path) {
    stroke-width: 2;
  }
  .code-stream {
    min-height: 1.3rem;
    white-space: nowrap;
    overflow: hidden;
    color: var(--tone-ram);
    font: 0.65rem/1.5 var(--font-code);
  }
  .diagram-source {
    margin-top: 0.4rem;
  }
  summary {
    color: var(--ink-2);
    cursor: pointer;
    font: 0.65rem var(--font-code);
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--ink-2);
    font: 0.65rem/1.6 var(--font-code);
  }
  summary:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  @media (prefers-reduced-motion: reduce) {
    .diagram-canvas :global(.node),
    .diagram-canvas :global(.edgePaths path) {
      transition: none;
    }
  }
  @media (max-width: 600px) {
    .diagram-canvas {
      min-height: 8rem;
    }
    .diagram-canvas :global(svg) {
      max-height: 26rem;
    }
  }
</style>
