// Shared rendering and metadata, with the hub's signed image URLs.
import { renderDoc as render } from "./docs.js";
import { signedDocsImgUrl } from "./docs-img.js";

export * from "./docs.js";

export function renderDoc(entry, slugByPath) {
  return render(entry, slugByPath, signedDocsImgUrl);
}
