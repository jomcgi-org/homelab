"""Run inside the deployed Hindsight API container as described in the README."""

import asyncio
import base64
import hashlib
import io
import json
import os
import secrets
from urllib.request import Request, urlopen

from hindsight_api.engine.retain.attachment_store import attachment_storage_key
from hindsight_api.engine.storage import create_file_storage
from PIL import Image, ImageDraw


def main():
    bank = "image-probe-" + secrets.token_hex(8)
    base = f"http://127.0.0.1:8888/v1/default/banks/{bank}"
    headers = {
        "Authorization": "Bearer " + os.environ["HINDSIGHT_API_TENANT_API_KEY"],
        "Content-Type": "application/json",
    }

    def request(path, body=None, method="POST"):
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(
            Request(base + path, data, headers, method=method), timeout=300
        ) as r:
            return json.load(r) if method == "POST" else None

    code = str(100000 + secrets.randbelow(900000))
    image = Image.new("RGB", (210, 32), "white")
    ImageDraw.Draw(image).text((5, 8), "Locker code: " + code, fill="black")
    png = io.BytesIO()
    image.resize((840, 128), Image.Resampling.NEAREST).save(png, format="PNG")
    raw = png.getvalue()
    content = [
        {"type": "text", "text": "Remember the locker code on this sign."},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(raw).decode(),
            },
        },
    ]
    assert os.environ["HINDSIGHT_API_FILE_STORAGE_S3_BUCKET"] == "discord-media"
    assert (
        os.environ["HINDSIGHT_API_FILE_STORAGE_S3_ENDPOINT"]
        == "http://garage.garage.svc:3900"
    )
    print("Synthetic probe bank:", bank, flush=True)
    result = request(
        "/memories",
        {"items": [{"document_id": "sign", "content": content}], "async": False},
    )
    try:
        assert result["success"], "retain failed"
        key = attachment_storage_key(bank, "sign", hashlib.sha256(raw).hexdigest())
        assert asyncio.run(create_file_storage("s3").retrieve(key)) == raw, (
            "Garage object differs"
        )
        facts = request("/memories/recall", {"query": "What is the locker code?"})[
            "results"
        ]
        assert any(code in fact["text"] for fact in facts), (
            "image-only code missing from recalled facts"
        )
        print(
            "Garage returned the exact PNG and recall returned the code from its pixels."
        )
    finally:
        request("", method="DELETE")


if __name__ == "__main__":
    main()
