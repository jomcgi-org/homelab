import { signedUrl } from "./imgproxy.js";

const PRESETS = new Set(["display", "full"]);

// key is a KnowledgeChunk's bucket-relative image_key.
export function signedGrimImgUrl(key, preset = "display") {
  return signedUrl({
    prefix: "s3://grimoire/",
    preset,
    presets: PRESETS,
    fallback: "display",
    key,
    label: "grimoire-img",
  });
}
