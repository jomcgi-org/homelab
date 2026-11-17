import type { Plugin } from "vite";
import type { PostSummary } from "./src/blog-types";
export const summaries: PostSummary[];
export function blogContent(): Plugin;
