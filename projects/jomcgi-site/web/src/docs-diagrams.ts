// Build-only browser entry. This module and Mermaid never enter dist.
import mermaid from "mermaid";
import { describeDiagram, plainText } from "./docs-diagram-description";

export async function renderDiagrams() {
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    htmlLabels: false,
    flowchart: { htmlLabels: false },
    fontFamily: getComputedStyle(document.body).fontFamily,
    fontSize: 16,
  });
  const sources = document.querySelectorAll<HTMLPreElement>("pre.doc-mermaid");
  for (const [index, source] of [...sources].entries()) {
    // Parsing or rendering errors fail the build, rather than quietly shipping
    // code where a diagram belongs. Keep the exact source available to readers.
    const text = source.textContent ?? "";
    const id = `docs-diagram-${index + 1}`;
    const diagram = await mermaid.mermaidAPI.getDiagramFromText(text);
    const { kind, statements } = describeDiagram(diagram);
    const heading = [
      ...document.querySelectorAll(
        "#doc-h, #doc-body h2, #doc-body h3, #doc-body h4",
      ),
    ].findLast(
      (heading) =>
        heading.compareDocumentPosition(source) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    );
    const name =
      plainText(diagram.db.getAccTitle?.() ?? "") ||
      `${kind}: ${heading?.textContent ?? document.title}`;
    const { svg } = await mermaid.render(id, text);
    const container = document.createElement("div");
    container.className = "docs-diagram";
    container.innerHTML = svg;
    source.before(container);
    // Retain the layout Mermaid measured before removing its theme. SVG
    // presentation attributes preserve geometry without inline CSS or paint.
    const defaults: Record<string, string> = {
      "text-anchor": "start",
      "dominant-baseline": "auto",
      "font-size": "16px",
      "font-weight": "400",
      "font-style": "normal",
      "stroke-width": "1px",
      "stroke-dasharray": "none",
      "stroke-linecap": "butt",
      "stroke-linejoin": "miter",
      opacity: "1",
      "fill-opacity": "1",
      "stroke-opacity": "1",
    };
    const geometry = [...container.querySelectorAll<SVGElement>("*")].map(
      (element) => {
        const computed = getComputedStyle(element);
        return {
          element,
          values: Object.entries(defaults).map(([property, fallback]) => [
            property,
            computed.getPropertyValue(property),
            fallback,
          ]),
        };
      },
    );
    for (const { element, values } of geometry) {
      for (const [property, value, fallback] of values) {
        if (value && value !== fallback) element.setAttribute(property!, value);
      }
    }
    container.querySelectorAll("style").forEach((style) => style.remove());
    for (const element of container.querySelectorAll<SVGElement>("*")) {
      element.removeAttribute("style");
      for (const property of ["fill", "stroke", "color"]) {
        if (element.getAttribute(property) !== "none")
          element.removeAttribute(property);
      }
    }
    const rendered = container.querySelector("svg")!;
    rendered.setAttribute("role", "img");
    rendered.removeAttribute("aria-label");
    rendered
      .querySelectorAll(":scope > title, :scope > desc")
      .forEach((element) => element.remove());
    const title = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "title",
    );
    title.id = `${id}-title`;
    title.textContent = name;
    const description = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "desc",
    );
    description.id = `${id}-description`;
    description.textContent = statements.join("\n");
    rendered.prepend(title, description);
    rendered.setAttribute("aria-labelledby", title.id);
    rendered.setAttribute("aria-describedby", description.id);

    // Also offer the equivalent as ordinary text for readers who need to zoom,
    // select or navigate it a connection at a time. It works without scripts.
    const equivalent = document.createElement("details");
    equivalent.className = "docs-diagram-description";
    const label = document.createElement("summary");
    label.textContent = "Diagram description";
    const list = document.createElement("ul");
    for (const statement of statements) {
      const item = document.createElement("li");
      item.textContent = statement;
      list.append(item);
    }
    equivalent.append(label, list);
    container.after(equivalent);
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Diagram source";
    details.append(summary);
    source.replaceWith(details);
    details.append(source);
  }
  return document.getElementById("doc-body")!.outerHTML;
}
