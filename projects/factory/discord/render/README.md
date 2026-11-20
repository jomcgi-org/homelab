# factory-discord-render

POST a raw Vega-Lite JSON document to `/vega-lite` or UTF-8 Mermaid source to
`/mermaid` on port 8080. A successful request returns `image/png`. Requests
over 65,536 bytes receive HTTP 413. External inputs, unsupported specs and
outputs over 4096 pixels on either side receive HTTP 422. A conversion killed
at the five-second deadline receives HTTP 504.

The Bazel target `//projects/factory/discord/render:image` packages the Rust
server, native engines and fonts. The published factory chart pins its digest
and deploys the service with the bridge, in the bridge namespace. A Cilium
deny rule also covers the cluster's baseline DNS allowance.

Run `python3 fidelity.py http://localhost:8080` against the running service.
The six Mermaid fixtures come from mermaid-rs-renderer’s basic conformance
cases. mmdr 0.3.1 renders their labels, edges, tasks and pie slices with DejaVu
2.37. The suite compares the service output to those PNGs and a vl-convert
1.9.0 inline bar chart. Review the images when changing the engines or fonts.
