import type { HomePost } from "./src/home-data";
export const hub: URL;
export const posts: HomePost[];
export function latestPosts(manifest?: HomePost[]): HomePost[];
export const homeDefines: Record<string, string>;
