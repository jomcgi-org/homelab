export const REFRESH_MS: number;
export const FORECAST_REFRESH_MS: number;
export const STALE_MS: number;
export function timestamp(value: unknown): string | null;
export function isStale(fetchedAt: string | null, now: number): boolean;
export function forecastStatus(
  fetchedAt: string | null,
  latestFetchedAt: string | null,
  now: number,
): string;
export function oldestForecast(timestamps: (string | null)[]): string | null;

export const SNAPSHOT_STALE_MS: number;
export function snapshotStatus(generatedAt: string | null, now: number): string;
