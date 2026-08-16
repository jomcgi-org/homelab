"""Website snapshotter domain: render deployed pages to PNG for agent inspection.

ADR embervm/035, issue #4994 T5. An EmberVM task-class guest (``shotter``)
runs headless Chromium and renders one URL to a PNG on request; this domain
is the monolith side of that: URL validation (the second, independent layer
described in ADR embervm/035 section 4), the EmberVM dispatch client
(``shotter.client``), SeaweedFS blob storage for the rendered PNG
(``shotter.s3``), and the MCP tool that ties them together
(``shotter.mcp``). It is an MCP-only domain, like ``sandbox``: no HTTP routes
of its own.
"""
