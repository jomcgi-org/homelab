# Frank performance

`/app/frank` serves the committed `frank.html` document verbatim, including
its inline CSS and JavaScript. It has no Svelte page bundle or data endpoint.

The document loads its four original images from `cdn.jomcgi.dev`, which
failed DNS resolution when
[#7988](https://github.com/jomcgi-org/homelab/issues/7988) was filed. The text
LCP does not establish a complete first view. Restoring the intended host or
approved original assets remains in #7988; no replacement images or guessed
hostname have been introduced.

## Cache windows

The [endpoint](+server.js) sends
`Cache-Control: public, max-age=300`. Browser and shared freshness are five
minutes. There is no explicit SWR, SIE or Cloudflare-only header, so the code
grants no stale extension. Source content changes with the document and
deployment; the live countdown runs locally in the browser.

The acceptable publication delay remains in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960).

## Build budget

The complete document, including inline scripts and styles, has a 12,000-byte
gzip limit. This is a document budget, not a separate JavaScript closure.
See [the shared contract](../../../../../docs/public-app-cache-windows.md) for measurement targets and cache
boundary requirements.
