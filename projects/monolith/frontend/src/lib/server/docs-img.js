import { signedUrl } from "./imgproxy.js";

const PRESETS = new Set(["full"]);

// repoPath is the repository-relative path of the image (also its S3 key).
export function signedDocsImgUrl(repoPath) {
  return signedUrl({
    prefix: "s3://docs-assets/",
    preset: "full",
    presets: PRESETS,
    fallback: "full",
    key: repoPath,
    label: "docs-img",
  });
}
