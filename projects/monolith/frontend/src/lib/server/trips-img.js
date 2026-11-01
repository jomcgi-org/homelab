import { PRESETS } from "../trips/images.js";
import { signedUrl } from "./imgproxy.js";

// key is the TripPoint image value (the S3 object key for that photo).
export function signedImgUrl(key, preset = "gallery") {
  return signedUrl({
    prefix: "s3://monolith-trips/",
    preset,
    presets: PRESETS,
    fallback: "gallery",
    key,
    label: "trips-img",
  });
}
