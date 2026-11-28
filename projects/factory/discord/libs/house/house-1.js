// Bosun house design system, version 1 (projects/factory/discord/libs).
// Served at /_lib/house-1.js, after the pinned libraries. Published pages
// load it with an integrity hash, so never edit it: copy it to house-2.js. It draws icons
// (<i data-lucide="name">), Mermaid diagrams (pre.mermaid, ELK layout) and
// Vega-Lite charts (script.vega-lite JSON), themed from house-1.css, then
// sets data-ready on <html>.
(async () => {
  const css = getComputedStyle(document.documentElement);
  // Colour tokens use light-dark(), so resolve each through a probe element
  // to the colour of the current scheme.
  const probe = document.createElement("span");
  probe.style.display = "none";
  document.body.append(probe);
  const token = (name) => {
    if (!name.startsWith("--font") && name !== "--display") {
      probe.style.color = `var(${name})`;
      return getComputedStyle(probe).color;
    }
    return css.getPropertyValue(name).trim();
  };
  const font = token("--font");
  const series = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => token(`--s${n}`));
  const fail = (el, err) => {
    const p = document.createElement("p");
    p.className = "error";
    p.textContent = `Did not render: ${err && err.message ? err.message : err}`;
    el.after(p);
  };
  try {
    try {
      if (window.lucide) {
        lucide.createIcons({
          attrs: { "stroke-width": 1.75, "aria-hidden": "true" },
        });
      }
    } catch (err) {
      // Icons are decoration: diagrams and charts still render.
      console.error(err);
    }
    const diagrams = document.querySelectorAll("pre.mermaid");
    if (!window.mermaid) {
      diagrams.forEach((el) => fail(el, "the Mermaid library did not load"));
    } else if (diagrams.length) {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        layout: "elk",
        theme: "base",
        fontFamily: font,
        sequence: {
          mirrorActors: false,
          actorFontSize: 15,
          messageFontSize: 15,
          noteFontSize: 14,
        },
        themeVariables: {
          fontFamily: font,
          fontSize: "15px",
          background: token("--surface"),
          primaryColor: token("--node"),
          primaryBorderColor: token("--node-border"),
          primaryTextColor: token("--text"),
          secondaryColor: token("--muted-bg"),
          tertiaryColor: token("--group"),
          lineColor: token("--diagram-line"),
          textColor: token("--text"),
          clusterBkg: token("--group"),
          clusterBorder: token("--edge"),
          edgeLabelBackground: token("--surface"),
          actorBkg: token("--node"),
          actorBorder: token("--node-border"),
          noteBkgColor: token("--panel-muted"),
          noteBorderColor: token("--accent"),
        },
      });
      for (const el of diagrams) {
        try {
          await mermaid.run({ nodes: [el] });
        } catch (err) {
          fail(el, err);
        }
      }
    }
    const text = {
      labelColor: token("--muted"),
      titleColor: token("--muted"),
      labelFont: font,
      titleFont: font,
      labelFontSize: 12,
      titleFontSize: 12,
    };
    for (const el of document.querySelectorAll(
      'script[type="application/json"].vega-lite',
    )) {
      const div = document.createElement("div");
      div.className = "chart";
      el.after(div);
      try {
        if (!window.vegaEmbed)
          throw new Error("the Vega libraries did not load");
        const spec = JSON.parse(el.textContent);
        // Fill the figure unless the spec sets its own width.
        if (spec.width === undefined) spec.width = "container";
        await vegaEmbed(div, spec, {
          actions: false,
          renderer: "svg",
          config: {
            background: null,
            font,
            autosize: { type: "fit-x", contains: "padding" },
            view: { stroke: null },
            axis: {
              ...text,
              domainColor: token("--edge"),
              tickColor: token("--edge"),
              gridColor: token("--line"),
            },
            axisX: { grid: false, labelAngle: 0 },
            legend: text,
            title: {
              color: token("--text"),
              font: token("--display"),
              fontSize: 15,
              fontWeight: 600,
              anchor: "start",
            },
            text: { color: token("--text"), font },
            mark: { color: token("--accent") },
            bar: { color: token("--accent"), cornerRadiusEnd: 4 },
            line: { color: token("--accent"), strokeWidth: 2.5 },
            point: { color: token("--accent"), filled: true },
            area: { color: token("--accent"), opacity: 0.25 },
            range: { category: series },
          },
        });
      } catch (err) {
        fail(div, err);
      }
    }
  } finally {
    document.documentElement.dataset.ready = "true";
  }
})();
