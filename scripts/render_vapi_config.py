"""
Render vapi_config/assistant_config.json (a template with {{BACKEND_URL}} and
{{VOICE_ID}} placeholders) into a real config using environment variables,
and optionally create/update the assistant on Vapi directly.

Never hardcodes secrets: BACKEND_URL, VAPI_VOICE_ID, and VAPI_API_KEY are all
read from the environment (see .env.example).

Usage:
    export BACKEND_URL=https://your-backend.example.com
    export VAPI_VOICE_ID=your-11labs-voice-id
    python scripts/render_vapi_config.py
        # writes vapi_config/assistant_config.rendered.json

    export VAPI_API_KEY=sk_...                 # optional
    python scripts/render_vapi_config.py --deploy
        # also POSTs to Vapi's API to create the assistant
        # (or PATCHes it if VAPI_ASSISTANT_ID is also set)
"""

import json
import os
import sys
import urllib.request

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "vapi_config", "assistant_config.json")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "vapi_config", "assistant_config.rendered.json")


def render():
    backend_url = os.environ.get("BACKEND_URL")
    voice_id = os.environ.get("VAPI_VOICE_ID")

    missing = [name for name, val in [("BACKEND_URL", backend_url), ("VAPI_VOICE_ID", voice_id)] if not val]
    if missing:
        print(f"ERROR: missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        print("Set them (see .env.example) and re-run.", file=sys.stderr)
        sys.exit(1)

    backend_url = backend_url.rstrip("/")

    with open(TEMPLATE_PATH, "r") as f:
        raw = f.read()

    raw = raw.replace("{{BACKEND_URL}}", backend_url).replace("{{VOICE_ID}}", voice_id)
    config = json.loads(raw)  # validates the substitution didn't break JSON
    config.pop("note", None)  # the template-only instructions field; not part of the real Vapi schema

    with open(OUTPUT_PATH, "w") as f:
        json.dump(config, f, indent=2)

    print(f"Wrote {OUTPUT_PATH}")
    return config


def deploy(config):
    api_key = os.environ.get("VAPI_API_KEY")
    if not api_key:
        print("VAPI_API_KEY not set — skipping deploy. Set it to create/update the assistant via Vapi's API,", file=sys.stderr)
        print("or paste vapi_config/assistant_config.rendered.json into the Vapi dashboard manually.", file=sys.stderr)
        return

    assistant_id = os.environ.get("VAPI_ASSISTANT_ID")
    if assistant_id:
        url = f"https://api.vapi.ai/assistant/{assistant_id}"
        method = "PATCH"
    else:
        url = "https://api.vapi.ai/assistant"
        method = "POST"

    body = json.dumps(config).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    print(f"{method} {url} -> assistant id: {result.get('id')}")
    if not assistant_id:
        print(f"Save this as VAPI_ASSISTANT_ID to update the same assistant next time: {result.get('id')}")


if __name__ == "__main__":
    cfg = render()
    if "--deploy" in sys.argv:
        deploy(cfg)
