// The web app manifest, one per host. app.html links /manifest.webmanifest
// and hooks.js reroutes it under the tier's prefix, so each tier names the
// app it installs: the private console is "dashboard", not the public site.
//
// The colours are the day sheet (technical-drawing.css --sheet). A manifest
// is static, so an installed app's splash is the day sheet even for a reader
// who stored the night scheme; the page repaints once it loads.
export function manifest(name, shortName) {
  return {
    name,
    short_name: shortName,
    start_url: "/",
    display: "standalone",
    background_color: "#ffffff",
    theme_color: "#ffffff",
    icons: [
      { src: "/favicon.svg", sizes: "any", type: "image/svg+xml" },
      {
        src: "/apple-touch-icon.png",
        sizes: "180x180",
        type: "image/png",
      },
    ],
  };
}

export function manifestResponse(name, shortName) {
  return new Response(JSON.stringify(manifest(name, shortName)), {
    headers: {
      "Content-Type": "application/manifest+json",
      "Cache-Control": "public, max-age=600",
    },
  });
}
