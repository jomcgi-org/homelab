import post from "virtual:blog/ember-conformance";
import { BlogPost } from "./blog-post";
import "virtual:blog/figures.css";
export function PostEmberConformancePage() {
  return <BlogPost post={post} />;
}
