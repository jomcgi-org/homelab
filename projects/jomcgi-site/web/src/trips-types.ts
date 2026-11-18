export interface Trip {
  slug: string;
  title: string;
  short_title?: string;
  subtitle?: string;
  default_image?: string;
  tz: string;
  default_zoom?: number;
  days: Record<string, { label?: string }>;
  highlights: { id: string; title: string; day: number; image?: string }[];
  stats: { coldest_temp?: number };
}
export interface Point {
  id: string;
  lat: number;
  lng: number;
  taken_at: string;
  image?: string;
  tags: string[];
  elevation?: number;
  iso?: number;
  shutter_speed?: string;
  aperture?: number;
  focal_length_35mm?: number;
}
export interface Day {
  dayNumber: number;
  date: string;
  points: Point[];
  distance: number;
  hasElevation: boolean;
  ascent: number;
  descent: number;
  minElevation: number | null;
  maxElevation: number | null;
}
export interface Payload {
  trip: Trip;
  points: Point[];
}
export interface Latest {
  generatedAt?: string;
  asOf: string | null;
  files: { index: string; trips: Record<string, string> };
  photoBase: string | null;
  refreshSeconds: number;
  photoCount: number;
  photoAssetCount: number;
  photoMode: "snapshot" | "public" | "private";
}
export interface Boot {
  renderedAt: number | null;
  asOf: string | null;
  refreshSeconds: number;
  photoBase: string | null;
  index: Trip[];
  trip?: Trip;
  dayNumber?: number;
  days: Omit<Day, "points">[];
  photos: Point[];
  photoCount: number;
  series: number[];
  distance: number;
  maxLat?: number;
  coldestTemp?: number;
}
