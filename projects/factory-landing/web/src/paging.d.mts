export function pageOf<T>(
  items: T[],
  requested: number,
): { items: T[]; page: number; pages: number };
