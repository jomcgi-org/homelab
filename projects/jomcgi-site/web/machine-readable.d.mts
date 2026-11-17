import type { HomePost } from "./src/home-data";
export function robots(): string;
export function sitemap(posts: HomePost[]): string;
export function llms(posts: HomePost[]): string;
export function webmanifest(): {
  name: string;
  short_name: string;
  start_url: string;
  display: string;
  background_color: string;
  theme_color: string;
  icons: { src: string; sizes: string; type: string }[];
};
