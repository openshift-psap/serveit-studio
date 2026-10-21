#!/usr/bin/env python3
"""Patch guidellm to set the x-llm-d-adapter routing header (RFE-2527).

The llm-d EPP's header-label-affinity scorer routes by the
``x-llm-d-adapter`` HTTP header — but guidellm conveys the adapter only
as ``model`` in the JSON body, so adapter-aware routing never engages
(benchmarks measure the fallback path: 0% adapter-pod affinity).

Applied at workload-image build time (docker/Dockerfile.workload). When
the request's ``model`` matches a configured ``lora_adapters`` entry,
the header is added before the request is sent. Base-model requests are
untouched.
"""

import sys
from pathlib import Path

ANCHOR = """        request_kwargs: dict[str, Any] = {
            "url": request_url,
            "method": arguments.method or "POST","""

PATCH = """        # ServeIt: RFE-2527 client-side contract — set the adapter routing
        # header when the request targets a LoRA adapter so the llm-d EPP's
        # header-label-affinity scorer can route by adapter locality.
        _adapters = set(getattr(self._args, "lora_adapters", None) or [])
        _model = (arguments.body or {}).get("model") \\
            if isinstance(arguments.body, dict) else None
        if _model and _adapters and _model in _adapters:
            arguments.headers = {
                **(arguments.headers or {}),
                "x-llm-d-adapter": _model,
            }

"""


def main() -> int:
    try:
        import guidellm
    except ImportError:
        print("guidellm not installed — nothing to patch", file=sys.stderr)
        return 1

    target = (
        Path(guidellm.__file__).parent / "backends" / "openai" / "http.py"
    )
    src = target.read_text()

    if "x-llm-d-adapter" in src:
        print(f"{target} already patched")
        return 0

    if ANCHOR not in src:
        print(
            "could not locate the request_kwargs anchor in http.py — "
            "guidellm version drift, aborting patch",
            file=sys.stderr,
        )
        return 1

    src = src.replace(ANCHOR, PATCH + ANCHOR, 1)
    target.write_text(src)
    print(f"patched {target}: adapter requests now set x-llm-d-adapter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
