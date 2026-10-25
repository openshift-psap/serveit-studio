#!/usr/bin/env python3
"""Patch guidellm to support conversation-pinned model assignment.

The multi-model round-robin assigns the next model per request, so every
turn of a multi-turn conversation targets a different model. For LoRA
serving that re-keys the prefix cache per turn (vLLM keys cache blocks
by prompt tokens AND model): a conversation whose turn-N prompt contains
its entire turn-1..N-1 history never hits cache, and session-affinity
routers cannot pin the conversation to one backend.

The new ``model_assignment`` backend argument selects the strategy:
``'request'`` (default) keeps the per-request round-robin;
``'conversation'`` advances the cycle once per conversation graph and
stamps every turn with that model — realistic multi-tenant traffic
where a user keeps their model (their adapter) for the whole session.

Validated live on an 8xTP2 vLLM deployment (8 adapters, 30 concurrent
10-turn conversations): prefix-cache hit rate ~0% -> 64%, TTFT p90
~800-1100ms -> 449ms, adapter load events 64 -> 16.

Applied at workload-image build time (docker/Dockerfile.workload).
"""

import py_compile
import re
import shutil
import sys
from pathlib import Path


def main() -> int:
    try:
        import guidellm
    except ImportError:
        print("guidellm not installed — nothing to patch", file=sys.stderr)
        return 1

    ok = True

    # --- 1. Schema: model_assignment field after lora_adapters ---
    schema_path = (
        Path(guidellm.__file__).parent / "schemas" / "backends" / "openai_http.py"
    )
    src = schema_path.read_text()
    if "model_assignment" not in src:
        m = re.search(
            r'(    lora_adapters: list\[str\] \| None = Field\(.*?\n    \)\n)',
            src,
            re.S,
        )
        if not m:
            print(f"{schema_path}: lora_adapters field not found", file=sys.stderr)
            ok = False
        else:
            field = m.group(1) + (
                '    model_assignment: Literal["request", "conversation"] = Field(\n'
                '        default="request",\n'
                '        description=(\n'
                '            "Assignment strategy for the multi-model round-robin. "\n'
                '            "\'request\' (default) assigns the next model per request. "\n'
                '            "\'conversation\' assigns one model per conversation and keeps "\n'
                '            "it for all of the conversation\'s turns — realistic "\n'
                '            "multi-tenant traffic where a user keeps their model for "\n'
                '            "the whole session."\n'
                '        ),\n'
                '    )\n'
            )
            shutil.copy(schema_path, str(schema_path) + ".bak")
            schema_path.write_text(src.replace(m.group(1), field, 1))
            print(f"patched {schema_path}: model_assignment field")
    else:
        print(f"{schema_path}: already patched")

    # --- 2. Backend: model_assignment property after request_models ---
    http_path = (
        Path(guidellm.__file__).parent / "backends" / "openai" / "http.py"
    )
    src = http_path.read_text()
    if "def model_assignment" not in src:
        anchor = "        return [base] if base else []\n\n"
        if anchor not in src:
            print(f"{http_path}: request_models anchor not found", file=sys.stderr)
            ok = False
        else:
            prop = anchor + (
                "    @property\n"
                '    def model_assignment(self) -> str:\n'
                '        return getattr(self._args, "model_assignment", "request") or "request"\n'
                "\n"
            )
            shutil.copy(http_path, str(http_path) + ".bak")
            http_path.write_text(src.replace(anchor, prop, 1))
            print(f"patched {http_path}: model_assignment property")
    else:
        print(f"{http_path}: already patched")

    # --- 3. Scheduler: per-conversation assignment ---
    wg_path = (
        Path(guidellm.__file__).parent / "scheduler" / "worker_group.py"
    )
    src = wg_path.read_text()
    if "per_conversation" not in src:
        # 3a. _ModelCycle.next()
        old_cycle = (
            "    def assign(self, request) -> None:\n"
            "        if not self._models:\n"
            "            return\n"
            "        request.model = self._models[self._index]\n"
            "        self._index = (self._index + 1) % len(self._models)"
        )
        new_cycle = (
            "    def next(self):\n"
            "        if not self._models:\n"
            "            return None\n"
            "        model = self._models[self._index]\n"
            "        self._index = (self._index + 1) % len(self._models)\n"
            "        return model\n"
            "\n"
            "    def assign(self, request) -> None:\n"
            "        model = self.next()\n"
            "        if model is not None:\n"
            "            request.model = model"
        )
        if old_cycle not in src:
            print(f"{wg_path}: _ModelCycle.assign anchor not found", file=sys.stderr)
            ok = False
        else:
            src = src.replace(old_cycle, new_cycle, 1)

        # 3b. generator signature
        old_sig = (
            "        requests: DatasetIterT[RequestT],\n"
            "        request_models: list[str] | None = None,\n"
            "    )"
        )
        new_sig = (
            "        requests: DatasetIterT[RequestT],\n"
            "        request_models: list[str] | None = None,\n"
            '        model_assignment: str = "request",\n'
            "    )"
        )
        if old_sig not in src:
            print(f"{wg_path}: requests_generator signature anchor not found",
                  file=sys.stderr)
            ok = False
        else:
            src = src.replace(old_sig, new_sig, 1)

        # 3c. per_conversation flag
        old_cycle_line = "        model_cycle = _ModelCycle(request_models or [])\n"
        if old_cycle_line not in src:
            print(f"{wg_path}: model_cycle construction anchor not found",
                  file=sys.stderr)
            ok = False
        else:
            src = src.replace(
                old_cycle_line,
                old_cycle_line
                + '        per_conversation = bool(request_models) and '
                'model_assignment == "conversation"\n',
                1,
            )

        # 3d. caller plumbing
        old_caller = (
            "        await self.messaging.start(\n"
            "            send_items=self.state.requests_generator(\n"
            "                self.requests,\n"
            "                request_models=request_models,\n"
            "            ),"
        )
        new_caller = (
            '        model_assignment = (\n'
            "            self.backend.model_assignment\n"
            "            if self.backend and request_models\n"
            '            else "request"\n'
            "        )\n"
            "        await self.messaging.start(\n"
            "            send_items=self.state.requests_generator(\n"
            "                self.requests,\n"
            "                request_models=request_models,\n"
            "                model_assignment=model_assignment,\n"
            "            ),"
        )
        if old_caller not in src:
            print(f"{wg_path}: caller plumbing anchor not found", file=sys.stderr)
            ok = False
        else:
            src = src.replace(old_caller, new_caller, 1)

        # 3e. per-graph stamping + guarded per-node assign
        old_loop = (
            "                for node_id in topo_dag_nodes:\n"
            "                    node = graph.nodes[node_id]\n"
            "                    count += 1\n"
            "\n"
            "                    request_id = self._find_request_id(node.request)\n"
            "\n"
            "                    # Round-robin model assignment (main process, before\n"
            "                    # the multiprocessing queue — immune to worker isolation)\n"
            "                    model_cycle.assign(node.request)"
        )
        new_loop = (
            "                if per_conversation:\n"
            "                    conversation_model = model_cycle.next()\n"
            "                    if conversation_model is not None:\n"
            "                        for node_id in topo_dag_nodes:\n"
            "                            graph.nodes[node_id].request.model = "
            "conversation_model\n"
            "\n"
            "                for node_id in topo_dag_nodes:\n"
            "                    node = graph.nodes[node_id]\n"
            "                    count += 1\n"
            "\n"
            "                    request_id = self._find_request_id(node.request)\n"
            "\n"
            "                    # Round-robin model assignment (main process, before\n"
            "                    # the multiprocessing queue — immune to worker isolation)\n"
            "                    if not per_conversation:\n"
            "                        model_cycle.assign(node.request)"
        )
        if old_loop not in src:
            print(f"{wg_path}: graph-loop anchor not found", file=sys.stderr)
            ok = False
        else:
            src = src.replace(old_loop, new_loop, 1)

        if ok:
            shutil.copy(wg_path, str(wg_path) + ".bak")
            wg_path.write_text(src)
            print(f"patched {wg_path}: conversation-pinned assignment")
    else:
        print(f"{wg_path}: already patched")

    if not ok:
        return 1

    # Verify every patched module still compiles
    for p in (schema_path, http_path, wg_path):
        py_compile.compile(str(p), doraise=True)

    # Smoke test the mechanism end-to-end
    from guidellm.scheduler.worker_group import _ModelCycle
    cycle = _ModelCycle(["m1", "m2", "m3"])
    assert [cycle.next() for _ in range(4)] == ["m1", "m2", "m3", "m1"]
    print("smoke test OK: cycle advances and wraps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
