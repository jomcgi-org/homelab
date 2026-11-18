import type { Payload } from "./trips-types";
const xml = (text: string) =>
  text.replace(/[<>&"']/g, (char) => `&#${char.charCodeAt(0)};`);
export function downloadRoute(data: Payload, type: "json" | "gpx") {
  const text =
    type === "json"
      ? JSON.stringify(data, null, 2)
      : `<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="jomcgi.dev" xmlns="http://www.topografix.com/GPX/1/1"><trk><name>${xml(data.trip.title)}</name><trkseg>${data.points.map((p) => `<trkpt lat="${p.lat}" lon="${p.lng}"><time>${xml(p.taken_at)}</time></trkpt>`).join("")}</trkseg></trk></gpx>`;
  const url = URL.createObjectURL(
    new Blob([text], {
      type: type === "json" ? "application/json" : "application/gpx+xml",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `${data.trip.slug}.${type}`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
