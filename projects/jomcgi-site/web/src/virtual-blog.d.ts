declare module "virtual:blog/figure-styles" {
  const css: string;
  export default css;
}
declare module "virtual:blog/index" {
  const posts: import("./blog-types").PostSummary[];
  export default posts;
}
declare module "virtual:blog/*" {
  const post: import("./blog-types").Post;
  export default post;
}
declare module "*.svelte" {
  const component: import("svelte").Component<any>;
  export default component;
}
