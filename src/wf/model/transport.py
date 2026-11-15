"""Default HTTP transport, standard library only."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from wf.model import ModelError


def post_json(url: str, headers: dict[str, str], body: dict, timeout: float = 300.0) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Keep the error short. Never echo request headers, which carry the API key.
        detail = e.read().decode(errors="replace")[:300]
        raise ModelError(f"model endpoint returned HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise ModelError(f"could not reach model endpoint: {e.reason}") from None
