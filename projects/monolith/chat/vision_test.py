"""Tests for the OpenAI-compatible vision client."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.vision import VisionClient


@pytest.fixture
def client():
    return VisionClient(base_url="http://fake:8080")


class TestVisionClient:
    @pytest.mark.asyncio
    async def test_describe_returns_text(self, client):
        """describe() returns the provider's text description."""
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.raise_for_status = MagicMock()
        fake_response.json.return_value = {
            "choices": [{"message": {"content": "A photo of a sunset over the ocean"}}]
        }

        with patch("chat.vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.return_value = fake_response
            mock_client_cls.return_value = mock_client

            result = await client.describe(b"\x89PNG\r\n", "image/png")

        assert result == "A photo of a sunset over the ocean"

    @pytest.mark.asyncio
    async def test_describe_sends_base64_image(self, client):
        """describe() sends the image as base64 in the vision content array."""
        image_bytes = b"\x89PNG\r\n"
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.raise_for_status = MagicMock()
        fake_response.json.return_value = {
            "choices": [{"message": {"content": "A picture"}}]
        }

        with patch("chat.vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.return_value = fake_response
            mock_client_cls.return_value = mock_client

            await client.describe(image_bytes, "image/png")

        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        messages = payload["messages"]
        user_msg = messages[-1]
        assert isinstance(user_msg["content"], list)
        image_part = [p for p in user_msg["content"] if p["type"] == "image_url"][0]
        expected_b64 = base64.b64encode(image_bytes).decode()
        assert f"data:image/png;base64,{expected_b64}" in image_part["image_url"]["url"]
