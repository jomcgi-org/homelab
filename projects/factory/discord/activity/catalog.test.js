import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Poll } from "./catalog.js";

test("the poll renders text and buttons without model URLs or HTML", () => {
  const url = "https://model.example/image";
  const components = [
    { component: "Image", id: "option2", url },
    {
      component: "Text",
      id: "question",
      text: "<img>",
      dangerouslySetInnerHTML: { __html: "<img>" },
    },
    {
      component: "Text",
      id: "option1",
      text: "Friday",
      href: url,
      src: url,
      style: { backgroundImage: "url(" + url + ")" },
    },
  ];
  const markup = renderToStaticMarkup(
    createElement(Poll, { components, counts: { option1: 2 } }),
  );
  assert.match(markup, /&lt;img&gt;/);
  assert.match(markup, /Friday \(2\)/);
  assert.doesNotMatch(markup, /model\.example|<img|<a[ >]|<link[ >]|<iframe/);
});
