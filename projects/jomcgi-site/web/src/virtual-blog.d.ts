declare module "virtual:blog/index" {
  const posts: import("./blog-types").PostSummary[];
  export default posts;
}
