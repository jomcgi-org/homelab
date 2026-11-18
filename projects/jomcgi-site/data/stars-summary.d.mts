export interface Summary {
  asOf: string | null;
  snapshotAsOf: string | null;
  count: number;
  renderedAt: number;
  rows: { name: string; hours: number; twilight: boolean }[];
}
export const REFRESH_MS: number;
export const FORECAST_REFRESH_MS: number;
export const EMPTY_SUMMARY: Summary;
export const SUMMARY_START: string;
export const SUMMARY_END: string;
export function makeSummary(
  snapshot: {
    fetched_at?: string | null;
    sites: {
      name?: string;
      lat: number;
      lon: number;
      clear_dark_hours?: number;
      clear_twilight_hours?: number;
    }[];
    count?: number;
    darkness?: string;
  },
  now?: number,
  snapshotAsOf?: string | null,
): Summary;
export function isStale(
  asOf: string | null,
  now: number,
  refreshMs?: number,
): boolean;
export function summaryHtml(summary: Summary, now?: number): string;
export function stampSummary(html: string, summary: Summary): string;
