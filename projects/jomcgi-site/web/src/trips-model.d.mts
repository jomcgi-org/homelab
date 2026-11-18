import type { Day, Point, Trip } from "./trips-types";
export function dayKey(iso: string, tz?: string): string | null;
export function haversineKm(a: Point, b: Point): number;
export function routeDistanceKm(points: Point[]): number;
export function groupByDay(points: Point[], tz?: string): Day[];
export function dayLabel(days: Trip["days"], day: number): string;
export function dayPhotos(points: Point[]): Point[];
export function clampIndex(index: number, count: number): number;
export function elevationSeries(points: Point[], maxPoints?: number): number[];
