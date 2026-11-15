import { expect, it, vi } from "vitest";
import { render } from "svelte/server";
import PhotoGrid from "./PhotoGrid.svelte";

it("inlines the first six photos and the complete no-JavaScript gallery", () => {
  const photos = Array.from({ length: 44 }, (_, index) => ({
    id: `photo-${index}`,
    imgGallery: `/gallery-${index}.jpg`,
  }));
  const fetch = vi.fn(() => {
    throw new Error("SSR must use the supplied photo URLs");
  });
  vi.stubGlobal("fetch", fetch);
  try {
    const { body } = render(PhotoGrid, { props: { photos } });
    const scripted = body.replace(/<noscript>[\s\S]*?<\/noscript>/g, "");
    expect([...scripted.matchAll(/<img /g)]).toHaveLength(6);
    expect([...scripted.matchAll(/loading="eager"/g)]).toHaveLength(6);
    expect([...scripted.matchAll(/fetchpriority="high"/g)]).toHaveLength(6);
    expect([...body.matchAll(/<img /g)]).toHaveLength(44);
    expect([...body.matchAll(/<button /g)]).toHaveLength(44);
    for (const photo of photos)
      expect(body).toContain(`src="${photo.imgGallery}"`);
    expect(scripted).toContain('src="/gallery-2.jpg"');
    expect(scripted).not.toContain('src="/gallery-6.jpg"');
    expect(fetch).not.toHaveBeenCalled();
  } finally {
    vi.unstubAllGlobals();
  }
});
