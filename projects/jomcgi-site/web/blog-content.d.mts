import type { Plugin } from "vite";
import type { PostSummary } from "./src/blog-types";
export const manifest: (PostSummary & { content: string })[];
export const summaries: PostSummary[];
export function blogContent(): Plugin;
