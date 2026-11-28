"""Run inside the dedicated actor: python3 workers_ai_probe.py {granted,ungranted}."""

import argparse
import json
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

RUN = "https://api.cloudflare.com/client/v4/accounts/7c56b458cd657d96b095c63d181c051f/ai/run/@cf/"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def call(method, model):
    request = Request(
        RUN + model,
        method=method,
        data=b'{"messages":[{"role":"user","content":"Reply with the word hello."}]}'
        if method == "POST"
        else None,
        headers={
            "Authorization": "Bearer placeholder",
            "Content-Type": "application/json",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=90) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("actor", choices=("granted", "ungranted"))
    actor = parser.parse_args().actor
    status, body = call("POST", "cloudflare/clef-flash")
    if actor == "ungranted":
        if status != 403:
            raise SystemExit(
                f"Expected access denial for ungranted actor, got {status}"
            )
        return
    if status != 200:
        raise SystemExit(f"Clef-flash returned HTTP {status}")
    payload = json.loads(body)
    answer = payload.get("result", {}).get("response")
    if (
        payload.get("success") is not True
        or not isinstance(answer, str)
        or not answer.strip()
    ):
        raise SystemExit("Clef-flash did not return an answer")
    for method, model in (
        ("POST", "meta/llama-3.1-8b-instruct"),
        ("GET", "cloudflare/clef-flash"),
    ):
        status, body = call(method, model)
        if status != 403 or body.strip() != b"Workers AI route denied":
            raise SystemExit(
                f"Missing gateway denial for {method} {model} (HTTP {status})"
            )


if __name__ == "__main__":
    main()
