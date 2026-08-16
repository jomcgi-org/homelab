"""BDD tests for shotter domain MCP tool registration and URL validation.

The shotter domain exposes a website snapshotter tool via MCP. These specs
define the tool's contract before implementation:

1. The tool is discoverable in the MCP catalogue
2. Host validation is strict: only jomcgi.dev and private.jomcgi.dev
3. Viewport dimensions are bounded
4. The tool returns an ImageContent block with embedded PNG plus metadata
5. EmberVM dispatch is mocked and tested without a live cluster
"""

from __future__ import annotations

import pytest

from shared.testing.markers import covers_route


class TestShotterToolDiscoverability:
    """The shotter MCP tool is registered and discoverable."""

    @pytest.mark.asyncio
    async def test_tool_is_registered_in_mcp_catalogue(self):
        """The shotter screenshot tool appears in the MCP tool list."""
        from shotter.mcp import register_mcp_tools

        # Register the shotter MCP tools
        register_mcp_tools()

        from core.mcp_app import mcp

        tools = await mcp.list_tools()
        tool_names = [tool.name for tool in tools]

        # Assert the screenshot tool is discoverable
        assert "screenshot_jomcgi_dev" in tool_names or "screenshot" in tool_names, (
            "shotter screenshot tool not found in MCP catalogue"
        )

    @pytest.mark.asyncio
    async def test_tool_description_passes_context_forge_validation(self):
        """The shotter tool description survives Context Forge sanitization.

        Context Forge blocks tool descriptions containing shell metacharacters
        or patterns like '&&', ';', '||', '$(', '|', '> ', '< '. This spec
        asserts that the tool's docstring does not sneak in forbidden patterns.
        """
        from shotter.mcp import register_mcp_tools

        register_mcp_tools()
        from core.mcp_app import mcp

        tools = await mcp.list_tools()

        # Find the shotter tool
        shotter_tool = None
        for tool in tools:
            if "screenshot" in tool.name.lower():
                shotter_tool = tool
                break

        assert shotter_tool is not None, "shotter tool not found"

        desc = shotter_tool.description or ""
        forbidden = ["&&", ";", "||", "$(", "|", "> ", "< "]
        for pat in forbidden:
            assert pat not in desc, (
                f"shotter tool description contains forbidden pattern {pat!r}: {desc}"
            )

        # Description length cap (Context Forge default: 8192)
        assert len(desc) <= 8192, (
            f"shotter tool description too long ({len(desc)} > 8192): {desc}"
        )


class TestHostValidation:
    """Host validation is strict: only jomcgi.dev and private.jomcgi.dev."""

    def test_accepts_public_host_jomcgi_dev(self):
        """The tool accepts https://jomcgi.dev URLs."""
        from shotter.mcp import validate_screenshot_url

        # Should not raise
        validate_screenshot_url("https://jomcgi.dev/", width=1024, height=768)

    def test_accepts_public_host_with_path_and_query(self):
        """The tool accepts jomcgi.dev URLs with paths and query strings."""
        from shotter.mcp import validate_screenshot_url

        # Should not raise
        validate_screenshot_url(
            "https://jomcgi.dev/agents?tab=overview", width=1024, height=768
        )

    def test_accepts_private_host(self):
        """The tool accepts https://private.jomcgi.dev URLs."""
        from shotter.mcp import validate_screenshot_url

        # Should not raise
        validate_screenshot_url("https://private.jomcgi.dev/", width=1024, height=768)

    def test_accepts_private_host_with_path(self):
        """The tool accepts private.jomcgi.dev URLs with paths."""
        from shotter.mcp import validate_screenshot_url

        validate_screenshot_url(
            "https://private.jomcgi.dev/private", width=1024, height=768
        )

    def test_rejects_completely_different_host(self):
        """The tool rejects completely unrelated hosts (credential injection risk).

        Section 4 of ADR 035 highlights that a different host entirely risks
        credential injection by the egress sidecar. api.github.com is named
        explicitly as an example of what the sidecar would credential.
        """
        from shotter.mcp import validate_screenshot_url, InvalidShotterURL

        with pytest.raises(
            InvalidShotterURL,
            match=r"(api\.github\.com|not a recognized host|unknown host)",
        ):
            validate_screenshot_url(
                "https://api.github.com/user", width=1024, height=768
            )

    def test_rejects_suffix_lookalike_evil_jomcgi_dev(self):
        """The tool rejects evil-jomcgi.dev (suffix lookalike).

        The egress allowlist is exact, not prefix or suffix matching. A host
        like evil-jomcgi.dev would bypass the allowlist if we only checked
        suffix match against jomcgi.dev.
        """
        from shotter.mcp import validate_screenshot_url, InvalidShotterURL

        with pytest.raises(InvalidShotterURL, match=r"(not allowed|not permitted)"):
            validate_screenshot_url("https://evil-jomcgi.dev/", width=1024, height=768)

    def test_rejects_domain_as_suffix_jomcgi_dev_evil_com(self):
        """The tool rejects jomcgi.dev.evil.com (domain as suffix).

        The allowlist does not tolerate our domain name as a suffix of
        another domain.
        """
        from shotter.mcp import validate_screenshot_url, InvalidShotterURL

        with pytest.raises(InvalidShotterURL, match=r"(not allowed|not permitted)"):
            validate_screenshot_url(
                "https://jomcgi.dev.evil.com/", width=1024, height=768
            )

    def test_rejects_unmapped_subdomain_staging_jomcgi_dev(self):
        """The tool rejects staging.jomcgi.dev (subdomain not in allowlist).

        Only the two exact hosts are allowed: jomcgi.dev and private.jomcgi.dev.
        No other subdomain is mapped to an internal service.
        """
        from shotter.mcp import validate_screenshot_url, InvalidShotterURL

        with pytest.raises(InvalidShotterURL, match=r"(not allowed|not permitted)"):
            validate_screenshot_url(
                "https://staging.jomcgi.dev/", width=1024, height=768
            )

    def test_rejects_non_https_scheme_http(self):
        """The tool rejects http:// (non-HTTPS scheme).

        Only HTTPS is allowed.
        """
        from shotter.mcp import validate_screenshot_url, InvalidShotterURL

        with pytest.raises(InvalidShotterURL, match=r"(HTTPS|scheme|protocol)"):
            validate_screenshot_url("http://jomcgi.dev/", width=1024, height=768)

    def test_rejects_file_scheme(self):
        """The tool rejects file:// URLs."""
        from shotter.mcp import validate_screenshot_url, InvalidShotterURL

        with pytest.raises(InvalidShotterURL, match=r"(scheme|protocol)"):
            validate_screenshot_url("file:///etc/passwd", width=1024, height=768)

    def test_rejects_data_url_scheme(self):
        """The tool rejects data: URLs."""
        from shotter.mcp import validate_screenshot_url, InvalidShotterURL

        with pytest.raises(InvalidShotterURL, match=r"(scheme|protocol)"):
            validate_screenshot_url(
                "data:text/html,<h1>test</h1>", width=1024, height=768
            )

    def test_rejects_url_with_embedded_credentials(self):
        """The tool rejects URLs with embedded user:pass credentials."""
        from shotter.mcp import validate_screenshot_url, InvalidShotterURL

        with pytest.raises(InvalidShotterURL, match=r"(credential|userinfo|@)"):
            validate_screenshot_url(
                "https://user:pass@jomcgi.dev/", width=1024, height=768
            )

    def test_rejects_url_with_non_standard_port(self):
        """The tool rejects URLs with an explicit non-standard port.

        The internal services are on port 3000, and the URL should use the
        implicit HTTPS port (443) or no port. An explicit port like
        jomcgi.dev:8080 is not permitted.
        """
        from shotter.mcp import validate_screenshot_url, InvalidShotterURL

        with pytest.raises(InvalidShotterURL, match=r"(port|expected|standard)"):
            validate_screenshot_url("https://jomcgi.dev:8080/", width=1024, height=768)


class TestViewportValidation:
    """Viewport dimensions are bounded to prevent abuse or runaway resource use."""

    def test_accepts_reasonable_viewport_1024x768(self):
        """Reasonable viewport dimensions are accepted."""
        from shotter.mcp import validate_screenshot_url

        # Should not raise
        validate_screenshot_url("https://jomcgi.dev/", width=1024, height=768)

    def test_accepts_large_but_bounded_viewport_3840x2160(self):
        """Large but bounded viewports (e.g. 4K) are accepted."""
        from shotter.mcp import validate_screenshot_url

        # 4K resolution should be acceptable
        validate_screenshot_url("https://jomcgi.dev/", width=3840, height=2160)

    def test_rejects_absurdly_large_width(self):
        """The tool rejects absurdly large width (e.g. 100000 pixels).

        Oversized requests are rejected rather than silently clamped or
        truncated, so the caller knows the request failed.
        """
        from shotter.mcp import validate_screenshot_url, InvalidViewportDimension

        with pytest.raises(InvalidViewportDimension, match=r"(too large|exceeds|max)"):
            validate_screenshot_url("https://jomcgi.dev/", width=100000, height=768)

    def test_rejects_absurdly_large_height(self):
        """The tool rejects absurdly large height."""
        from shotter.mcp import validate_screenshot_url, InvalidViewportDimension

        with pytest.raises(InvalidViewportDimension, match=r"(too large|exceeds|max)"):
            validate_screenshot_url("https://jomcgi.dev/", width=1024, height=100000)

    def test_rejects_zero_width(self):
        """The tool rejects zero width."""
        from shotter.mcp import validate_screenshot_url, InvalidViewportDimension

        with pytest.raises(
            InvalidViewportDimension, match=r"(positive|non-zero|minimum)"
        ):
            validate_screenshot_url("https://jomcgi.dev/", width=0, height=768)

    def test_rejects_zero_height(self):
        """The tool rejects zero height."""
        from shotter.mcp import validate_screenshot_url, InvalidViewportDimension

        with pytest.raises(
            InvalidViewportDimension, match=r"(positive|non-zero|minimum)"
        ):
            validate_screenshot_url("https://jomcgi.dev/", width=1024, height=0)

    def test_rejects_negative_width(self):
        """The tool rejects negative width."""
        from shotter.mcp import validate_screenshot_url, InvalidViewportDimension

        with pytest.raises(
            InvalidViewportDimension, match=r"(positive|non-zero|negative)"
        ):
            validate_screenshot_url("https://jomcgi.dev/", width=-1024, height=768)


class TestEmberVMDispatch:
    """The tool dispatches to the EmberVM shotter workload and mocks integration."""

    @pytest.fixture(autouse=True)
    def _mock_embervm(self, monkeypatch):
        """Mock the EmberVM HTTP client for hermetic testing."""
        from shotter import client as shotter_client

        class _FakeResponse:
            status_code = 200
            text = ""

            def raise_for_status(self):
                pass

            def json(self):
                # Return the correct shotter guest response shape per issue #4994 T3:
                # {png_b64, width, height, final_url, status, duration_ms}
                return {
                    "png_b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                    "width": 1024,
                    "height": 768,
                    "final_url": "https://jomcgi.dev/",
                    "status": 200,
                    "duration_ms": 2500,
                }

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, json=None, headers=None):
                # Verify the request is going to the shotter workload
                assert "shotter" in url, f"expected shotter in URL, got {url}"
                return _FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        monkeypatch.setenv("EMBERVM_URL", "http://mock-embervm")

    @pytest.mark.asyncio
    async def test_dispatches_to_shotter_workload(self):
        """The tool dispatches to the EmberVM shotter task workload.

        A fresh task is created per screenshot request, ensuring isolation.
        """
        from shotter.client import capture

        result = await capture(
            url="https://jomcgi.dev/",
            width=1024,
            height=768,
            timeout_ms=30000,
        )

        # Assert the returned dict has the expected shotter guest response shape
        assert isinstance(result, dict)
        assert "png_b64" in result
        assert len(result["png_b64"]) > 0
        assert result["width"] == 1024
        assert result["height"] == 768
        assert result["final_url"] == "https://jomcgi.dev/"
        assert result["status"] == 200
        assert result["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_includes_idempotency_key_header(self, monkeypatch):
        """The EmberVM request includes an Idempotency-Key header.

        Idempotent requests ensure safe retry semantics.
        """
        from shotter import client as shotter_client

        captured_headers = {}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, json=None, headers=None):
                captured_headers.update(headers or {})

                class Resp:
                    status_code = 200
                    text = ""

                    def raise_for_status(self):
                        pass

                    def json(self):
                        return {
                            "png_b64": "test",
                            "width": 1024,
                            "height": 768,
                            "final_url": "https://jomcgi.dev/",
                            "status": 200,
                            "duration_ms": 1000,
                        }

                return Resp()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        monkeypatch.setenv("EMBERVM_URL", "http://mock-embervm")

        await shotter_client.capture(
            url="https://jomcgi.dev/",
            width=1024,
            height=768,
            timeout_ms=30000,
        )

        assert "Idempotency-Key" in captured_headers


class TestMCPReturnShape:
    """The MCP tool returns an ImageContent block with PNG data plus metadata."""

    @pytest.mark.asyncio
    async def test_screenshot_returns_image_content_with_metadata(self, monkeypatch):
        """The tool returns a mcp.types.ImageContent block with PNG data.

        ADR 035 section 5 deliberately returns a real ImageContent block
        (not base64 in a dict) because models render image blocks more
        reliably than JSON base64. The ImageContent includes the rendered
        PNG in the data field and metadata (url, final_url, status, etc.)
        in the meta field.

        Testability constraint this pins: `shotter.mcp` must reach the
        client through the module (`client.capture(...)`), not via a
        `from shotter.client import capture` bound at import time. A
        from-import binds the name in `shotter.mcp` before monkeypatch can
        replace it, so the patch below would silently miss and the tool
        would attempt a real EmberVM dispatch from a unit test.
        """
        from shotter import client as shotter_client
        from shotter.mcp import screenshot_url

        one_px_png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )

        async def _fake_capture(**kwargs):
            return {
                "png_b64": one_px_png,
                "width": 1024,
                "height": 768,
                "final_url": "https://jomcgi.dev/",
                "status": 200,
                "duration_ms": 2500,
            }

        monkeypatch.setattr(shotter_client, "capture", _fake_capture)

        result = await screenshot_url(
            url="https://jomcgi.dev/",
            width=1024,
            height=768,
            timeout_ms=30000,
        )

        # Result must be a mcp.types.ImageContent block
        from mcp.types import ImageContent

        assert isinstance(result, ImageContent), (
            f"expected ImageContent, got {type(result)}"
        )

        # Image must be PNG (correct camelCase field name)
        assert result.mimeType == "image/png", (
            f"expected image/png, got {result.mimeType}"
        )

        # Image data must be present and non-empty (base64-encoded)
        assert result.data is not None and len(result.data) > 0, (
            "ImageContent data must be non-empty base64-encoded PNG"
        )

        # Metadata must include URL and other response fields
        assert result.meta is not None, "ImageContent meta must be present"
        assert "url" in result.meta, "meta must include stored SeaweedFS url"
        assert result.meta["url"], "stored URL must be non-empty"
        assert "final_url" in result.meta
        assert "status" in result.meta
        assert result.meta["status"] == 200
        assert "width" in result.meta
        assert result.meta["width"] == 1024
        assert "height" in result.meta
        assert result.meta["height"] == 768

    @pytest.mark.asyncio
    async def test_timeout_surfaces_as_real_error(self, monkeypatch):
        """A slow page that exceeds timeout surfaces as a real tool error.

        Timeout nesting ensures the tool error is not a severed connection
        or a generic timeout from Context Forge, but a clear error from
        the tool itself.

        Section 5 of ADR 035: Context Forge TOOL_TIMEOUT (60s) >
        monolith client read > workload timeoutSeconds > guest handler cap >
        CDP navigate timeout. Each strictly inside the one above.
        """
        from shotter.client import capture, ToolTimeout

        # Mock a transport that times out immediately
        class _TimeoutClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, json=None, headers=None, timeout=None):
                # Simulate a timeout by raising asyncio.TimeoutError
                import asyncio

                raise asyncio.TimeoutError("Read timed out")

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)
        monkeypatch.setenv("EMBERVM_URL", "http://mock-embervm")

        # The client must convert the transport timeout into a ToolTimeout
        with pytest.raises(ToolTimeout):
            await capture(
                url="https://jomcgi.dev/",
                width=1024,
                height=768,
                timeout_ms=100,
            )


class TestHostToServiceMapping:
    """Public hostnames map to the correct internal services.

    This is monolith's own copy of the mapping, used to validate a requested
    URL and to tell the guest which internal service to dial. It is the
    SECOND of the two layers in ADR embervm/035 section 4, not the primary
    one. The primary control is the in-guest proxy's hard allowlist, which is
    Go running inside the Firecracker guest and is covered by a Go test in
    that package, not from here. A Python spec cannot observe the guest's
    behaviour, and asserting on a Python stand-in would prove the shape of
    the seam while proving nothing about what the guest actually enforces.

    Every assertion below is on the EXACT mapped value. Substring matching
    is not good enough here: "monolith" is a substring of
    "monolith-public-frontend", so a loose check passes even when the
    private tier is wired to the public service. Near-miss matching is the
    precise failure this mapping exists to prevent.
    """

    def test_jomcgi_dev_maps_to_public_frontend(self):
        """jomcgi.dev maps to the public frontend service, exactly.

        The public tier is served from a separate service in a separate
        namespace.
        """
        from shotter.hosts import HOST_SERVICE_MAP

        assert (
            HOST_SERVICE_MAP["jomcgi.dev"]
            == "monolith-public-frontend.monolith-public.svc.cluster.local:3000"
        )

    def test_private_jomcgi_dev_maps_to_private_frontend(self):
        """private.jomcgi.dev maps to the monolith frontend service, exactly."""
        from shotter.hosts import HOST_SERVICE_MAP

        assert (
            HOST_SERVICE_MAP["private.jomcgi.dev"]
            == "monolith.monolith.svc.cluster.local:3000"
        )

    def test_map_contains_only_allowed_hosts(self):
        """The host map contains only the two allowed hosts, no others.

        Monolith cannot dispatch a host it has no internal service for,
        which is what makes the mapping itself a validation boundary rather
        than just a lookup table. Adding a third entry is a deliberate
        security decision and should fail this test until it is recorded.
        """
        from shotter.hosts import HOST_SERVICE_MAP

        # Exactly two hosts in the map
        assert len(HOST_SERVICE_MAP) == 2, (
            f"HOST_SERVICE_MAP must have exactly 2 entries, got {len(HOST_SERVICE_MAP)}"
        )
        assert set(HOST_SERVICE_MAP.keys()) == {"jomcgi.dev", "private.jomcgi.dev"}, (
            f"HOST_SERVICE_MAP must contain only jomcgi.dev and private.jomcgi.dev, "
            f"got {set(HOST_SERVICE_MAP.keys())}"
        )
