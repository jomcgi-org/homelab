import { mount } from "svelte";
import App from "./App.svelte";
import "./fonts.css";
import data from "./fixtures/blog-page.json";
import { assertBlogPage } from "./blog-contract.js";
assertBlogPage(data);
mount(App, { target: document.getElementById("app") });
// The bounded harness has one route. Preserve fragment interactions while
// keeping production breadcrumbs and source links inside this fixture.
document.addEventListener(
  "click",
  (event) => {
    const anchor = event.target.closest("a[href]");
    if (!anchor) return;
    const url = new URL(anchor.href, location.href);
    if (url.origin !== location.origin || url.pathname !== location.pathname) {
      event.preventDefault();
      document.getElementById("preview-scope").focus();
    }
  },
  true,
);
