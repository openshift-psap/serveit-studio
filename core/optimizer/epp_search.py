"""EPP routing search space: specs, dependency resolution, archetypes.

The llm-d inference scheduler (EPP) composes request routing from three
plugin kinds:

- producers: request-control data publishers (``approx-prefix-cache-producer``
  publishes per-request prefix match info; ``inflight-load-producer`` publishes
  in-flight token load per endpoint)
- scorers/filters: scheduling plugins that consume producer data or live
  endpoint metrics
- pickers: the final selection strategy (``max-score-picker`` argmax vs
  ``weighted-random-picker`` probabilistic)

Two properties of this plugin system drive everything in this module:

1. Scorers silently score 0 for every endpoint when their producer is
   absent (e.g. ``prefix-cache-scorer`` without ``approx-prefix-cache-producer``),
   so a spec is only meaningful after the producer dependency closure is
   applied.
2. The prefix-cache-affinity-filter narrows candidates to sticky endpoints
   and hands the final choice to downstream scorers. A weighted
   prefix-cache-scorer running after the filter attenuates its load gate
   (the filter README quantifies this: the prefix weight must be at least
   the sum of load scorer weights before the gate can ever break), so the
   recommended composition pairs the filter with a single load scorer and
   leaves the prefix scorer out.
"""

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Plugin registry (llm-d-inference-scheduler v0.11.0)
# ---------------------------------------------------------------------------

MAX_SCORE_PICKER = "max-score-picker"
WEIGHTED_RANDOM_PICKER = "weighted-random-picker"
PICKER_TYPES = {MAX_SCORE_PICKER, WEIGHTED_RANDOM_PICKER}

APPROX_PREFIX_PRODUCER = "approx-prefix-cache-producer"
INFLIGHT_LOAD_PRODUCER = "inflight-load-producer"
AFFINITY_FILTER = "prefix-cache-affinity-filter"

# spec scorer key -> EPP plugin type
SCORER_TYPES: Dict[str, str] = {
    "prefix_cache": "prefix-cache-scorer",
    "kv_cache": "kv-cache-utilization-scorer",
    "queue": "queue-scorer",
    "active_request": "active-request-scorer",
    "token_load": "token-load-scorer",
    "no_hit_lru": "no-hit-lru-scorer",
    "load_aware": "load-aware-scorer",
    "session_affinity": "session-affinity-scorer",
    "lora_affinity": "lora-affinity-scorer",
    "header_label_affinity": "header-label-affinity-scorer",
    "latency": "latency-scorer",
}

# Scorers whose score depends on a request-control producer.
NEEDS_APPROX_PRODUCER = {"prefix_cache", "no_hit_lru"}
NEEDS_INFLIGHT_PRODUCER = {"token_load"}

# Affinity-filter parameters with their v0.11.0 defaults.
AFFINITY_FILTER_DEFAULTS: Dict[str, object] = {
    "affinityThreshold": 0.80,
    "maxTTFTPenaltyMs": 18000,
    "explorationProbability": 0.0,
}

# Scorer runtime parameters supported by the spec (passed through to the
# plugin's `parameters` block).
SCORER_PARAM_KEYS: Dict[str, Tuple[str, ...]] = {
    "token_load": ("queueThresholdTokens",),
    "active_request": ("idleThreshold", "maxBusyScore"),
    "no_hit_lru": ("lruSize",),
    "load_aware": ("threshold",),
    "session_affinity": ("strategy", "sessionIdConfig"),
    "header_label_affinity": ("headerName", "headerValue"),
}


@dataclass
class EPPSpec:
    """A complete, renderable EPP routing configuration.

    Attributes:
        name: short identifier used in test ids and logs.
        phase: search phase that produced the spec (screen/ablate/weight/final).
        rationale: one-line human explanation of why this variant exists.
        arch: 'aggregated', 'pd' or 'ep'.
        picker: 'max-score-picker' or 'weighted-random-picker'.
        scorers: mapping of scorer key -> weight (>0). Absent means disabled.
        session_affinity_mode: None | 'filter' | 'scorer'. Weight (scorer
            mode) is taken from ``scorers['session_affinity']``.
        affinity_filter: parameter dict for prefix-cache-affinity-filter,
            or None to disable the filter. ``peakPrefillThroughput`` must
            be provided by the caller (hardware/model specific).
        scorer_params: per-scorer parameter overrides.
        base_epp: passthrough producer tuning (maxPrefixBlocksToMatch,
            lruCapacityPerServer, nonCachedTokens, prefixCacheAutoTune).
        slo_enabled: keep the SLO profile handler + latency-scorer wiring.
    """

    name: str
    arch: str
    phase: str = "screen"
    rationale: str = ""
    picker: str = MAX_SCORE_PICKER
    scorers: Dict[str, float] = field(default_factory=dict)
    session_affinity_mode: Optional[str] = None
    affinity_filter: Optional[Dict] = None
    scorer_params: Dict[str, Dict] = field(default_factory=dict)
    base_epp: Dict = field(default_factory=dict)
    slo_enabled: bool = False

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "arch": self.arch,
            "phase": self.phase,
            "rationale": self.rationale,
            "picker": self.picker,
            "scorers": dict(self.scorers),
            "session_affinity_mode": self.session_affinity_mode,
            "affinity_filter": dict(self.affinity_filter) if self.affinity_filter else None,
            "scorer_params": {k: dict(v) for k, v in self.scorer_params.items()},
            "base_epp": dict(self.base_epp),
            "slo_enabled": self.slo_enabled,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "EPPSpec":
        return cls(
            name=d["name"],
            arch=d.get("arch", "aggregated"),
            phase=d.get("phase", "screen"),
            rationale=d.get("rationale", ""),
            picker=d.get("picker", MAX_SCORE_PICKER),
            scorers=dict(d.get("scorers") or {}),
            session_affinity_mode=d.get("session_affinity_mode"),
            affinity_filter=dict(d["affinity_filter"]) if d.get("affinity_filter") else None,
            scorer_params={k: dict(v) for k, v in (d.get("scorer_params") or {}).items()},
            base_epp=dict(d.get("base_epp") or {}),
            slo_enabled=bool(d.get("slo_enabled", False)),
        )

    def copy(self, **changes) -> "EPPSpec":
        """dataclasses.replace with mutable fields copied (not shared)."""
        base = {
            "scorers": dict(self.scorers),
            "scorer_params": {k: dict(v) for k, v in self.scorer_params.items()},
            "affinity_filter": dict(self.affinity_filter) if self.affinity_filter else None,
            "base_epp": dict(self.base_epp),
        }
        base.update(changes)
        return replace(self, **base)

    # -- dependency resolution ----------------------------------------------

    def needed_producers(self) -> Tuple[List[str], List[str]]:
        """Producers this spec's plugins depend on: (approx, inflight)."""
        approx = bool(NEEDS_APPROX_PRODUCER & set(self.scorers)) or self.affinity_filter is not None
        inflight = bool(NEEDS_INFLIGHT_PRODUCER & set(self.scorers)) or self.affinity_filter is not None
        return approx, inflight

    def resolve(self) -> Tuple["EPPSpec", List[str]]:
        """Apply dependency + composition rules, returning the fixed spec.

        Rules:
        - the affinity filter excludes the weighted prefix-cache-scorer
          (its weight attenuates the filter's TTFT load gate);
        - producers are inferred from consumers (informational — the
          renderer always derives them the same way);
        - the filter's TTFT gate needs the inflight producer.
        """
        warnings: List[str] = []
        spec = self.copy()

        if spec.affinity_filter and spec.scorers.get("prefix_cache"):
            dropped = spec.scorers.pop("prefix_cache")
            warnings.append(
                f"dropped prefix-cache-scorer (weight {dropped}) — a weighted "
                f"prefix scorer attenuates the affinity filter's TTFT load gate"
            )

        if spec.picker not in PICKER_TYPES:
            warnings.append(f"unknown picker {spec.picker!r} — using {MAX_SCORE_PICKER}")
            spec.picker = MAX_SCORE_PICKER

        if spec.affinity_filter:
            params = dict(AFFINITY_FILTER_DEFAULTS)
            params.update(spec.affinity_filter)
            spec.affinity_filter = params

        spec.scorers = {
            k: (float(v) if v is not None else None) for k, v in spec.scorers.items() if v is None or float(v) > 0
        }
        return spec, warnings

    # -- rendering -----------------------------------------------------------

    def plugin_instances(self) -> List[Dict]:
        """Ordered plugin list for the configmap `plugins:` section.

        Order: producers, filters, scorers, picker. The profile handler is
        added by the template (it depends on SLO mode and architecture).
        """
        plugins: List[Dict] = []
        approx, inflight = self.needed_producers()
        base = self.base_epp or {}
        if approx:
            entry = {"type": APPROX_PREFIX_PRODUCER}
            params = {}
            if base.get("prefixCacheAutoTune") is not None:
                params["autoTune"] = bool(base.get("prefixCacheAutoTune"))
            if base.get("maxPrefixBlocksToMatch") is not None:
                params["maxPrefixBlocksToMatch"] = int(base["maxPrefixBlocksToMatch"])
            if base.get("lruCapacityPerServer") is not None:
                params["lruCapacityPerServer"] = int(base["lruCapacityPerServer"])
            if params:
                entry["parameters"] = params
            plugins.append(entry)
        if inflight:
            plugins.append({"type": INFLIGHT_LOAD_PRODUCER})

        if self.affinity_filter:
            plugins.append({"type": AFFINITY_FILTER, "parameters": dict(self.affinity_filter)})

        if self.session_affinity_mode == "filter":
            plugins.append({"type": "session-affinity-filter", "parameters": {"strategy": "session_id"}})
        elif self.session_affinity_mode == "scorer":
            # the mode is the enable flag; the weight rides in
            # scorers['session_affinity'] and is applied in the profile
            plugins.append({"type": "session-affinity-scorer", "parameters": {"strategy": "session_id"}})

        for key in _ordered_scorer_keys(self.scorers):
            if key == "session_affinity":
                # emitted above from session_affinity_mode
                continue
            entry = {"type": SCORER_TYPES[key]}
            params = _clean_params(key, self.scorer_params.get(key, {}))
            if key == "session_affinity":
                params.setdefault("strategy", "session_id")
            if params:
                entry["parameters"] = params
            plugins.append(entry)

        plugins.append({"type": self.picker})
        return plugins

    def profile_entries(self) -> List[Tuple[str, Optional[float]]]:
        """Ordered (pluginRef, weight) entries for the default/prefill profile.

        Filters and the picker take no weight. Scorers carry theirs.
        """
        entries: List[Tuple[str, Optional[float]]] = []
        if self.affinity_filter:
            entries.append((AFFINITY_FILTER, None))
        if self.session_affinity_mode == "filter":
            entries.append(("session-affinity-filter", None))
        elif self.session_affinity_mode == "scorer":
            entries.append(("session-affinity-scorer", self.scorers.get("session_affinity", 2.0)))
        for key in _ordered_scorer_keys(self.scorers):
            if key == "session_affinity":
                continue
            entries.append((SCORER_TYPES[key], self.scorers[key]))
        entries.append((self.picker, None))
        return entries

    def decode_profile_entries(self) -> List[Tuple[str, Optional[float]]]:
        """Decode-profile entries for pd/ep architectures.

        Decode targets are chosen for load spreading, not prefix affinity:
        active-request (or queue) plus the affinity plugins that must follow
        the request's session/adapter.
        """
        entries: List[Tuple[str, Optional[float]]] = []
        if self.session_affinity_mode == "filter":
            entries.append(("session-affinity-filter", None))
        elif self.session_affinity_mode == "scorer":
            entries.append(("session-affinity-scorer", self.scorers.get("session_affinity", 2.0)))
        if "lora_affinity" in self.scorers:
            entries.append(("lora-affinity-scorer", self.scorers["lora_affinity"]))
        entries.append(("active-request-scorer", self.scorers.get("active_request", 1.0)))
        entries.append((self.picker, None))
        return entries

    def to_epp_config(self) -> Dict:
        """Legacy-compatible epp_config dict for update_epp_config + DB.

        Carries both the structured spec (rendered via spec_mode) and the
        flat `plugins` view that older consumers (resume, step 10, report
        fallback) understand.
        """
        plugins: Dict[str, Dict] = {}
        for key, weight in self.scorers.items():
            if key == "session_affinity" and self.session_affinity_mode != "scorer":
                continue
            # None weight = render without a weight line (EPP defaults
            # to 1.0) — preserve it, don't crash on float(None)
            plugins[key] = {"enabled": True, "weight": None if weight is None else float(weight)}
        if self.session_affinity_mode:
            plugins["session_aware"] = {
                "enabled": True,
                "mode": self.session_affinity_mode,
                "weight": float(self.scorers.get("session_affinity") or 2.0),
            }
        cfg = {
            "preset": "custom",
            "spec": self.to_dict(),
            "plugins": plugins,
            "picker": self.picker,
        }
        if self.affinity_filter:
            cfg["prefixCacheAffinityEnabled"] = True
            if self.affinity_filter.get("peakPrefillThroughput") is not None:
                cfg["peakPrefillThroughput"] = self.affinity_filter["peakPrefillThroughput"]
            for k in ("affinityThreshold", "maxTTFTPenaltyMs", "explorationProbability"):
                if self.affinity_filter.get(k) is not None:
                    cfg[k] = self.affinity_filter[k]
        cfg.update(
            {
                k: v
                for k, v in (self.base_epp or {}).items()
                if k in ("maxPrefixBlocksToMatch", "lruCapacityPerServer", "nonCachedTokens", "prefixCacheAutoTune")
            }
        )
        return cfg

    def legacy_weights(self) -> Dict:
        """Weight dict shape used by epp_benchmark_results consumers."""
        w = {
            "prefix_cache_weight": self.scorers.get("prefix_cache", 0) or 0,
            "kv_cache_weight": self.scorers.get("kv_cache", 0) or 0,
            "queue_weight": self.scorers.get("queue", 0) or 0,
            "active_request_weight": self.scorers.get("active_request", 0) or 0,
            "slo_enabled": self.slo_enabled,
            "slo_weight": self.scorers.get("latency", 0) or 0,
            "token_load_weight": self.scorers.get("token_load", 0) or 0,
            "lora_affinity_weight": self.scorers.get("lora_affinity"),
            "session_aware_weight": (
                self.scorers.get("session_affinity") if self.session_affinity_mode == "scorer" else None
            ),
            "picker": self.picker,
            "affinity_filter": bool(self.affinity_filter),
            "phase": self.phase,
            "spec": self.to_dict(),
        }
        if self.session_affinity_mode == "filter":
            w["session_aware_enabled"] = True
            w["session_affinity_mode"] = "filter"
        return w

    def hash_key(self) -> str:
        payload = {
            "arch": self.arch,
            "picker": self.picker,
            "scorers": {k: round(float(v or 1.0), 3) for k, v in sorted(self.scorers.items())},
            "session_affinity_mode": self.session_affinity_mode,
            "affinity_filter": (self.affinity_filter and sorted(self.affinity_filter.items())) or None,
            "scorer_params": {k: sorted(v.items()) for k, v in sorted(self.scorer_params.items())},
            "base_epp": {k: self.base_epp[k] for k in sorted(self.base_epp)},
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]

    def describe(self) -> str:
        parts = []
        if self.affinity_filter:
            parts.append("affinity-filter")
        scorer_bits = [
            f"{k}×{v:g}" if v is not None else k
            for k, v in _ordered_scorer_items(self.scorers)
            if not (k == "session_affinity" and self.session_affinity_mode != "scorer")
        ]
        if self.session_affinity_mode:
            parts.append(f"session:{self.session_affinity_mode}")
        if scorer_bits:
            parts.append(", ".join(scorer_bits))
        parts.append(self.picker.replace("-picker", ""))
        return " | ".join(parts)


def _ordered_scorer_keys(scorers: Dict[str, float]) -> List[str]:
    return [k for k, _ in _ordered_scorer_items(scorers)]


def _ordered_scorer_items(scorers: Dict[str, float]) -> List[Tuple[str, float]]:
    """Affinity scorers first (they gate the candidate set), then load and
    distribution scorers by descending weight."""
    order = {
        "lora_affinity": 0,
        "header_label_affinity": 1,
        "latency": 2,
        "no_hit_lru": 3,
        "load_aware": 4,
        "token_load": 5,
        "active_request": 6,
        "queue": 7,
        "kv_cache": 8,
        "prefix_cache": 9,
        "session_affinity": 10,
    }
    return sorted(scorers.items(), key=lambda kv: (order.get(kv[0], 99), -(kv[1] or 1.0), kv[0]))


def _clean_params(scorer_key: str, params: Dict) -> Dict:
    allowed = SCORER_PARAM_KEYS.get(scorer_key, ())
    out = {}
    for k, v in (params or {}).items():
        if k in allowed and v is not None:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Archetypes and search moves
# ---------------------------------------------------------------------------


@dataclass
class SearchContext:
    """Workload facts the archetype/ablation generators reason over."""

    arch: str
    num_pods: int
    workload_bound: str = "prefill"  # 'prefill' | 'decode' | 'balanced'
    cache_mode: str = "identical"  # 'identical' | 'shared_prefix' | 'multi_group'
    cache_hit_pct: float = 0.0  # expected/measured prefix cache hit %
    session_aware: bool = False
    lora_enabled: bool = False
    slo_enabled: bool = False
    smart_weights: Dict = field(default_factory=dict)
    user_weights: Dict = field(default_factory=dict)
    user_spec: Optional["EPPSpec"] = None  # user's custom config as a spec (control arm)
    affinity_filter_params: Dict = field(default_factory=dict)  # user-set filter params
    peak_prefill_throughput: Optional[float] = None
    base_epp: Dict = field(default_factory=dict)


def archetype_optimized_baseline(ctx: SearchContext) -> EPPSpec:
    """llm-d optimized-baseline guide: hard affinity filter + one load scorer.

    Prefill-bound workloads pair the filter with token-load-scorer (uncached
    token cost), decode-bound with active-request-scorer. Producers are
    inferred by the dependency closure.
    """
    load_scorer = "token_load" if ctx.workload_bound != "decode" else "active_request"
    weight = 1.0
    scorers = {load_scorer: weight}
    if ctx.lora_enabled:
        scorers["lora_affinity"] = ctx.smart_weights.get("lora_affinity_weight", 3.0) or 3.0
    aff = dict(AFFINITY_FILTER_DEFAULTS)
    aff.update(ctx.affinity_filter_params or {})
    if ctx.peak_prefill_throughput:
        aff["peakPrefillThroughput"] = ctx.peak_prefill_throughput
    spec = EPPSpec(
        name="opt-baseline",
        arch=ctx.arch,
        phase="screen",
        rationale=(
            "llm-d optimized-baseline guide: prefix-cache-affinity-filter gates "
            f"candidates, {SCORER_TYPES[load_scorer]} ranks the survivors"
        ),
        picker=MAX_SCORE_PICKER,
        scorers=scorers,
        session_affinity_mode=("filter" if ctx.session_aware else None),
        affinity_filter=aff,
        base_epp=ctx.base_epp,
        slo_enabled=ctx.slo_enabled,
    )
    return spec


def archetype_weighted(ctx: SearchContext, picker: str) -> EPPSpec:
    """Classic weighted multi-scorer stack (the pre-overhaul tuner's space).

    Weights come from the smart derivation when available, else the user
    preset. This is the composition michey-style specs use.
    """
    weights = dict(ctx.smart_weights or ctx.user_weights or {})
    scorers: Dict[str, float] = {}
    for key, wkey in (
        ("prefix_cache", "prefix_cache_weight"),
        ("kv_cache", "kv_cache_weight"),
        ("queue", "queue_weight"),
        ("active_request", "active_request_weight"),
        ("token_load", "token_load_weight"),
    ):
        w = float(weights.get(wkey) or 0)
        if w > 0:
            scorers[key] = w
    if ctx.lora_enabled:
        scorers["lora_affinity"] = float(weights.get("lora_affinity_weight") or 3.0)
    name = "weighted-max" if picker == MAX_SCORE_PICKER else "weighted-rand"
    return EPPSpec(
        name=name,
        arch=ctx.arch,
        phase="screen",
        rationale=(f"weighted multi-scorer stack, smart-derived weights, {picker}"),
        picker=picker,
        scorers=scorers,
        session_affinity_mode=("scorer" if ctx.session_aware else None),
        base_epp=ctx.base_epp,
        slo_enabled=ctx.slo_enabled,
    )


def spec_from_custom_config(epp: Dict, arch: str) -> Optional["EPPSpec"]:
    """Reconstruct the user's custom EPP config as a spec.

    Reads the same flat schema the UI writes (plugins dict + top-level
    affinity-filter keys), preserving structure: absent scorers stay
    absent, the filter carries the user's parameters.
    """
    if not epp:
        return None
    plugins = epp.get("plugins") or {}
    scorers: Dict[str, float] = {}
    for key in (
        "prefix_cache",
        "kv_cache",
        "queue",
        "active_request",
        "token_load",
        "no_hit_lru",
        "load_aware",
        "lora_affinity",
        "header_label_affinity",
    ):
        p = plugins.get(key)
        if p is None:
            continue  # not mentioned in the config — excluded
        if not p.get("enabled", True):
            continue  # explicitly disabled
        w = p.get("weight")
        if w is None:
            # enabled with no weight — the EPP defaults scorers to 1.0
            scorers[key] = None
        elif float(w) > 0:
            scorers[key] = float(w)
    aff = None
    if epp.get("prefixCacheAffinityEnabled"):
        # pre-fill defaults: an empty dict is falsy and would silently drop
        # the filter from rendering (params the user set override)
        aff = dict(AFFINITY_FILTER_DEFAULTS)
        for k in ("affinityThreshold", "maxTTFTPenaltyMs", "peakPrefillThroughput", "explorationProbability"):
            if epp.get(k) is not None:
                aff[k] = epp[k]
    sess = plugins.get("session_aware") or {}
    mode = None
    if sess.get("enabled"):
        mode = sess.get("mode", "filter")
        if mode == "scorer":
            scorers.setdefault("session_affinity", float(sess.get("weight", 2) or 2))
    if not scorers and aff is None and mode is None:
        return None
    return EPPSpec(
        name="preset",
        arch=arch,
        phase="screen",
        rationale="user custom EPP config (control arm)",
        picker=epp.get("picker", MAX_SCORE_PICKER),
        scorers=scorers,
        session_affinity_mode=mode,
        affinity_filter=aff,
    )


def archetype_preset_mirror(ctx: SearchContext) -> EPPSpec:
    """The user's selection, expressed as a spec (control arm).

    A custom config mirrors faithfully (structure + parameters); preset
    selections mirror by weight.
    """
    if ctx.user_spec is not None:
        return ctx.user_spec.copy(base_epp=ctx.base_epp, slo_enabled=ctx.slo_enabled)
    weights = dict(ctx.user_weights or {})
    scorers: Dict[str, float] = {}
    for key, wkey in (
        ("prefix_cache", "prefix_cache_weight"),
        ("kv_cache", "kv_cache_weight"),
        ("queue", "queue_weight"),
        ("active_request", "active_request_weight"),
    ):
        w = float(weights.get(wkey) or 0)
        if w > 0:
            scorers[key] = w
    if ctx.lora_enabled:
        scorers["lora_affinity"] = float(weights.get("lora_affinity_weight") or 3.0)
    return EPPSpec(
        name="preset",
        arch=ctx.arch,
        phase="screen",
        rationale="user preset structure (control arm for the screening phase)",
        picker=MAX_SCORE_PICKER,
        scorers=scorers,
        session_affinity_mode=("filter" if ctx.session_aware else None),
        base_epp=ctx.base_epp,
        slo_enabled=ctx.slo_enabled,
    )


def phase1_specs(ctx: SearchContext) -> List[EPPSpec]:
    """Archetype screening set. All specs resolve to valid compositions."""
    specs: List[EPPSpec] = []

    # The filter narrows hardest in multi_group (each pod is warm only for
    # the groups it served — requests find their group's pods); in
    # identical/shared_prefix most pods are sticky so it gates little; with
    # no measured/configured reuse it would no_match to a no-op.
    cache_relevant = ctx.cache_hit_pct > 0
    if cache_relevant and ctx.num_pods > 1:
        specs.append(archetype_optimized_baseline(ctx))

    specs.append(archetype_weighted(ctx, MAX_SCORE_PICKER))
    if ctx.num_pods > 2:
        specs.append(archetype_weighted(ctx, WEIGHTED_RANDOM_PICKER))
    specs.append(archetype_preset_mirror(ctx))

    seen = set()
    unique = []
    for s in specs:
        r, _ = s.resolve()
        h = r.hash_key()
        if h not in seen:
            seen.add(h)
            unique.append(r)
    return unique


def ablation_candidates(winner: EPPSpec, ctx: SearchContext) -> List[EPPSpec]:
    """Workload-driven add/remove moves on the screening winner."""
    candidates: List[EPPSpec] = []

    def _add(name, rationale, **changes):
        base = winner.copy(phase="ablate", name=name, rationale=rationale)
        for k, v in changes.items():
            setattr(base, k, v)
        candidates.append(base)

    # ── Removal ablations first: stress-test the winner's defining
    # structure. If removing a plugin doesn't hurt the objective, it was
    # dead weight — and the isolation answers "did THIS plugin matter",
    # which whole-composition comparisons (phase 1) cannot.
    if winner.affinity_filter:
        _add(
            "abl-filter-off",
            "remove the affinity filter — does stickiness actually pay, or is the load scorer doing all the work?",
            affinity_filter=None,
        )
    if winner.session_affinity_mode:
        _add(
            "abl-session-off",
            "remove session affinity — is the pin buying anything at this pod count?",
            session_affinity_mode=None,
        )

    # Session affinity for stateful/multi-turn workloads
    if ctx.session_aware and not winner.session_affinity_mode:
        _add("abl-session", "multi-turn workload — add session-affinity filter", session_affinity_mode="filter")

    # Cold-request spreading when there is real prefix reuse
    if ctx.cache_hit_pct > 0 and "no_hit_lru" not in winner.scorers and not winner.affinity_filter:
        _add(
            "abl-nohit-lru",
            "cold requests spread cache growth — add no-hit-lru",
            scorers={**winner.scorers, "no_hit_lru": 1.0},
        )

    # KV-cache distribution signal when off
    if "kv_cache" not in winner.scorers:
        _add("abl-kv", "add kv-cache-utilization distribution signal", scorers={**winner.scorers, "kv_cache": 1.0})

    # LoRA-affinity A/B: half weight and off (adapter locality mechanism)
    if ctx.lora_enabled and winner.scorers.get("lora_affinity"):
        cur = float(winner.scorers.get("lora_affinity") or 3.0)
        _add(
            "abl-lora-half",
            f"adapter locality — lora-affinity {cur:g} → {cur / 2:g}",
            scorers={**winner.scorers, "lora_affinity": max(0.5, round(cur / 2, 1))},
        )
        no_lora = {k: v for k, v in winner.scorers.items() if k != "lora_affinity"}
        _add("abl-lora-off", "adapter locality — lora-affinity disabled", scorers=no_lora)

    # Swap the load scorer for the other bound
    if ctx.workload_bound == "prefill" and "token_load" not in winner.scorers and not winner.affinity_filter:
        _add(
            "abl-token-load",
            "prefill-bound workload — add token-load-scorer",
            scorers={**winner.scorers, "token_load": 1.0},
        )
    elif ctx.workload_bound == "decode" and "active_request" not in winner.scorers:
        _add(
            "abl-active-req",
            "decode-bound workload — add active-request-scorer",
            scorers={**winner.scorers, "active_request": 1.0},
        )

    out = []
    for c in candidates:
        r, _ = c.resolve()
        if r.hash_key() != winner.hash_key():
            out.append(r)
    return out


def weight_neighbors(winner: EPPSpec, ordered_keys: Optional[List[str]] = None) -> List[EPPSpec]:
    """Coordinate-descent moves: ±1.0 on the two highest-weight scorers."""
    if winner.affinity_filter:
        # The optimized-baseline composition is parameter-light by design:
        # a single load scorer + filter. Explore its load weight and the
        # filter's stickiness threshold instead of stacking more scorers.
        return _affinity_filter_neighbors(winner)

    keys = ordered_keys or [k for k, _ in _ordered_scorer_items(winner.scorers) if k != "session_affinity"][:2]
    moves: List[EPPSpec] = []
    for key in keys:
        cur = float(winner.scorers.get(key) or 1.0)  # None weight = EPP default 1.0
        for delta in (-1.0, 1.0):
            new = round(max(0.5, min(5.0, cur + delta)), 1)
            if abs(new - cur) < 0.01:
                continue
            scorers = {**winner.scorers, key: new}
            if new <= 0:
                scorers.pop(key)
            spec = winner.copy(
                name=f"w-{key}-{new:g}".replace(".", "p"),
                phase="weight",
                rationale=f"coordinate move: {key} {cur:g} → {new:g}",
                scorers=scorers,
            ).resolve()[0]
            if spec.hash_key() != winner.hash_key():
                moves.append(spec)
    return moves


def _affinity_filter_neighbors(winner: EPPSpec) -> List[EPPSpec]:
    """Moves for the filter composition: load-scorer weight and threshold."""
    moves: List[EPPSpec] = []
    load_key = (
        "token_load"
        if "token_load" in winner.scorers
        else ("active_request" if "active_request" in winner.scorers else None)
    )
    if load_key:
        cur = float(winner.scorers.get(load_key) or 1.0)  # None weight = EPP default 1.0
        for new in (round(cur * 2, 1), max(0.5, round(cur / 2, 1))):
            if abs(new - cur) < 0.01 or new > 5:
                continue
            spec = winner.copy(
                name=f"w-load-{new:g}".replace(".", "p"),
                phase="weight",
                rationale=f"filter composition load weight {cur:g} → {new:g}",
                scorers={**winner.scorers, load_key: new},
            ).resolve()[0]
            if spec.hash_key() != winner.hash_key():
                moves.append(spec)
    if winner.affinity_filter:
        cur_thr = float(winner.affinity_filter.get("affinityThreshold", 0.80))
        for new_thr in (0.6, 0.9):
            if abs(new_thr - cur_thr) < 0.01:
                continue
            aff = {**winner.affinity_filter, "affinityThreshold": new_thr}
            spec = winner.copy(
                name=f"w-thr-{new_thr:g}".replace(".", "p"),
                phase="weight",
                rationale=f"affinity threshold {cur_thr:g} → {new_thr:g} "
                f"(lower = looser stickiness, higher = stricter)",
                affinity_filter=aff,
            ).resolve()[0]
            if spec.hash_key() != winner.hash_key():
                moves.append(spec)
    return moves


def final_spec(winner: EPPSpec) -> EPPSpec:
    """Relabel the winner for the full-duration validation phase."""
    return winner.copy(
        name=f"final-{winner.name}",
        phase="final",
        rationale=f"full-duration validation of the best screening variant ({winner.name})",
    )


# ---------------------------------------------------------------------------
# Configmap rendering (spec_mode)
# ---------------------------------------------------------------------------


def render_spec_body(spec: "EPPSpec", epp_v011_plus: bool = True) -> str:
    """Render the EndpointPickerConfig body for the configmap `data:` block.

    Returns YAML text indented by 4 spaces (the depth of the configmap
    data block), covering plugins + schedulingProfiles including the
    architecture-specific handler chains:

    - aggregated: single-profile-handler (or slo-aware) + `default` profile
    - pd: always-disagg-pd-decider + disagg-profile-handler + prefill/decode
    - ep: prefix-based-pd-decider + disagg-profile-handler + prefill/decode
    """
    import yaml

    plugins: List[Dict] = []
    profiles: List[Dict] = []
    arch = spec.arch

    if arch == "aggregated":
        if spec.slo_enabled:
            if not epp_v011_plus:
                plugins.append({"type": "slo-request-tracker"})
            plugins.append({"type": "slo-aware-profile-handler"})
        else:
            plugins.append({"type": "single-profile-handler"})
        plugins.extend(spec.plugin_instances())
        profiles.append(_profile("default", spec.profile_entries()))
        if spec.slo_enabled and spec.scorers.get("latency"):
            profiles.append(
                _profile(
                    "slo",
                    [
                        ("prefix-cache-scorer", 0),
                        ("latency-scorer", spec.scorers["latency"]),
                        (spec.picker, None),
                    ],
                )
            )
    else:
        decider = "always-disagg-pd-decider" if arch == "pd" else "prefix-based-pd-decider"
        non_cached = (spec.base_epp or {}).get("nonCachedTokens")
        decider_params = {"nonCachedTokens": int(non_cached)} if non_cached is not None else None
        if arch == "pd":
            plugins.append({"type": decider})
            if not epp_v011_plus:
                plugins.insert(0, {"type": "disagg-headers-handler"})
            handler_params = {"deciders": {"prefill": decider}} if epp_v011_plus else {"deciderPluginName": decider}
            plugins.append({"type": "disagg-profile-handler", "parameters": handler_params})
            if decider_params is not None:
                plugins.append({"type": "prefix-based-pd-decider", "parameters": decider_params})
        else:
            entry = {"type": decider}
            if decider_params is not None:
                entry["parameters"] = decider_params
            plugins.append(entry)
            handler_params = {"deciders": {"prefill": decider}} if epp_v011_plus else {"deciderPluginName": decider}
            plugins.append({"type": "disagg-profile-handler", "parameters": handler_params})
        plugins.append({"type": "prefill-filter"})
        plugins.append({"type": "decode-filter"})
        plugins.extend(spec.plugin_instances())
        prefill_entries = [("prefill-filter", None)] + spec.profile_entries()
        decode_entries = [("decode-filter", None)] + spec.decode_profile_entries()
        # closure: every pluginRef used by a profile must be defined in the
        # plugins list (the decode profile implies e.g. active-request-scorer
        # even when the spec's scorer set doesn't include it)
        defined = {p["type"] for p in plugins}
        for ref, _w in prefill_entries + decode_entries:
            if ref in defined:
                continue
            entry = {"type": ref}
            if ref == "session-affinity-scorer":
                entry["parameters"] = {"strategy": "session_id"}
            plugins.append(entry)
            defined.add(ref)
        profiles.append(_profile("prefill", prefill_entries))
        profiles.append(_profile("decode", decode_entries))

    # apiVersion/kind are emitted by the configmap template itself — the
    # body must not repeat them (the EPP's strict YAML decoder rejects
    # duplicate keys).
    config = {
        "plugins": plugins,
        "schedulingProfiles": profiles,
    }
    text = yaml.safe_dump(config, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True)
    # pyyaml emits indentless sequences ("plugins:" then "- type:" at the same
    # column); re-indent to the nested style the handwritten configmaps use
    # ("plugins:" then "  - type:") so kubectl output reads the same either way.
    # Top-level keys stay put; sequence dashes and their children shift +2.
    lines = []
    for line in text.splitlines():
        if not line.strip():
            lines.append("")
            continue
        top_key = not line.startswith(" ") and not line.startswith("-")
        lines.append(line if top_key else "  " + line)
    return "\n".join(("    " + l) if l else "" for l in lines) + "\n"


def _profile(name: str, entries: List[Tuple[str, Optional[float]]]) -> Dict:
    plugins = []
    for ref, weight in entries:
        entry: Dict = {"pluginRef": ref}
        if weight is not None:
            entry["weight"] = float(weight)
        plugins.append(entry)
    return {"name": name, "plugins": plugins}


def describe_config_delta(base_weights: Dict, base_structure: Dict, var_weights: Dict, var_structure: Dict) -> str:
    """Compact 'what changed' diff between two report EPP entries.

    Operates on the (weights, structure) dicts the report parser produces
    (from as-run configmaps or stored specs), not on EPPSpec — so it works
    for baseline rows too. Empty string when nothing changed.
    """
    base_w = dict(base_weights or {})
    var_w = dict(var_weights or {})
    base_st = dict(base_structure or {})
    var_st = dict(var_structure or {})
    parts: List[str] = []

    # affinity filter
    base_f = bool(base_st.get("affinity_filter"))
    var_f = bool(var_st.get("affinity_filter"))
    if var_f and not base_f:
        parts.append("+filter")
    elif base_f and not var_f:
        parts.append("−filter")

    # scorers added / removed
    keys = [k for k in set(base_w) | set(var_w) if k not in ("session_affinity",)]
    for k in sorted(keys):
        b = base_w.get(k) or 0
        v = var_w.get(k) or 0
        if b and not v:
            parts.append(f"−{k.replace('_', '-')}×{b:g}")
        elif v and not b:
            parts.append(f"+{k.replace('_', '-')}×{v:g}")

    # weight changes on shared scorers
    for k in sorted(keys):
        b = base_w.get(k) or 0
        v = var_w.get(k) or 0
        if b and v and abs(float(b) - float(v)) >= 0.05:
            parts.append(f"{k.replace('_', '-')} {b:g}→{v:g}")

    # picker
    bp = base_st.get("picker")
    vp = var_st.get("picker")
    if bp and vp and bp != vp:
        short = lambda p: "w-rand" if "weighted" in p else ("max" if "max" in p else p)
        parts.append(f"picker {short(bp)}→{short(vp)}")

    return " ".join(parts)
