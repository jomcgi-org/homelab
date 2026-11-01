// Synthetic data shared by the actual-route DOM test and browser preview.
export const attacks = [
  "<img src=x onerror=alert(1)>",
  "<script>alert('synthetic')</script>",
  "[x](javascript:alert(1))",
];
export const safeMarkdown = [
  "# Synthetic heading",
  "## Second heading",
  "### Third heading",
  "[Safe reference](https://example.com/notes)",
  `https://example.com/${"long-reference-".repeat(12)}`,
  "- Unordered first\n- Unordered second",
  "3. Ordered first\n4. Ordered second",
  '```js\nconst text = "<img src=x>";\n' +
    "const longLine = '" +
    "code".repeat(45) +
    "';\n```",
].join("\n\n");
// The first two attacks and unsafe URL cross text_chunk event boundaries.
export const chunks = [
  `${safeMarkdown}\n\n<img src=x on`,
  "error=alert(1)>\n\n<scr",
  "ipt>alert('synthetic')</script>\n\n[x](java",
  "script:alert(1))",
];
export const noteId = "synthetic note/7536";
export const graphEvent = {
  type: "node_discovered",
  data: {
    note_id: noteId,
    title: "Synthetic linked note",
    type: "note",
    tags: ["synthetic"],
    edges: [],
  },
};
export const note = {
  title: "Synthetic linked note",
  type: "note",
  tags: ["synthetic"],
  content: `---\nprivate_fixture: removed-frontmatter\n---\n${safeMarkdown}\n\n${attacks.join("\n\n")}\n\n## Links\nremoved-trailing-link`,
  edges: [],
};

export function createChatFixture() {
  const requests = [];
  let controller;
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(value) {
      controller = value;
    },
  });
  return {
    requests,
    emit(event) {
      controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
    },
    finish() {
      this.emit({ type: "done", data: {} });
      controller.close();
    },
    async fetch(input, init = {}) {
      const url = String(input);
      requests.push({ url, method: init.method ?? "GET", body: init.body });
      if (url === "/private/chat" && init.method === "POST") {
        return new Response(stream, {
          headers: { "content-type": "text/event-stream" },
        });
      }
      if (url === `/private/chat?note_id=${encodeURIComponent(noteId)}`) {
        return new Response(JSON.stringify(note), {
          headers: { "content-type": "application/json" },
        });
      }
      throw new Error(`Unexpected synthetic request: ${url}`);
    },
  };
}
