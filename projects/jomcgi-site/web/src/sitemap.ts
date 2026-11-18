import { HOME, ORIGIN, SECTIONS } from "./sections";

export function sitemap(entries: string[]): string {
  const priorities: Record<string, string> = {
    "/": "1.0",
    "/cv": "0.9",
    "/docs": "0.8",
    "/blog": "0.8",
  };
  const listed = [HOME, ...SECTIONS].filter(
    (section) => section.live && section.listed !== false,
  );
  const urls = entries.flatMap((entry) => {
    const path =
      "/" + entry.replace(/(^|\/)index\.html$/, "$1").replace(/\.html$/, "");
    const section = listed.find(
      ({ path: root }) => path === root || path.startsWith(root + "/"),
    );
    if (!section) return [];
    const priority =
      priorities[path] ?? (path.startsWith("/scotland/") ? "0.3" : "0.7");
    return [
      `  <url>\n    <loc>${ORIGIN}${path}</loc>\n    <priority>${priority}</priority>\n  </url>`,
    ];
  });
  return `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.join("\n")}
</urlset>
`;
}
