"""Dataset generation for optimization workloads."""

import os
import hashlib


class DatasetMixin:
    """Mixin providing dataset generation methods for RecipeOptimizer."""

    def _build_prompt_maker(self):
        """Build a prompt generation function using the model tokenizer if available."""
        vocab = None
        tokenizer = None
        try:
            from transformers import AutoTokenizer
            hf_home = os.environ.get('HF_HOME') or os.path.join(
                os.environ.get('HOME_STORAGE_DIR', '/mnt/storage'), '.cache', 'huggingface')
            hf_token = self.config.hf_token or os.environ.get('HF_TOKEN')
            tokenizer = AutoTokenizer.from_pretrained(
                self.config.model_name, trust_remote_code=True,
                cache_dir=hf_home, token=hf_token
            )
            vocab = [t for t in tokenizer.get_vocab().keys()
                     if len(t) > 2 and t.isascii() and t.isalpha()]
            if len(vocab) < 500:
                vocab = None
        except Exception:
            pass

        def make_prompt(length_tokens, rng_instance):
            if vocab and tokenizer:
                words = [rng_instance.choice(vocab) for _ in range(length_tokens * 2)]
                text = ' '.join(words)
                tokens = tokenizer.encode(text, add_special_tokens=False)
                if len(tokens) > length_tokens:
                    text = tokenizer.decode(tokens[:length_tokens], skip_special_tokens=True)
            elif vocab:
                words = [rng_instance.choice(vocab) for _ in range(length_tokens)]
                text = ' '.join(words)
            else:
                words = []
                for _ in range(int(length_tokens * 1.3)):
                    wlen = rng_instance.randint(3, 10)
                    words.append(''.join(rng_instance.choices('abcdefghijklmnopqrstuvwxyz', k=wlen)))
                text = ' '.join(words)
            return text

        return make_prompt

    def _dataset_mode(self):
        """Return the dataset generation mode based on config."""
        return 'corpus' if getattr(self.config, 'use_corpus', False) else 'random'

    def _compute_dataset_seed(self, **overrides):
        """Compute a deterministic seed from ALL dataset parameters.

        Includes model, ISL, OSL, stdev, mode, hit_pct, turns, corpus,
        first_prompt_tokens, prefix_tokens — everything that affects
        the generated dataset. Two different configs always produce
        different seeds.
        """
        cfg = self.config
        parts = [
            str(overrides.get('model', cfg.model_name)),
            str(overrides.get('isl', cfg.isl)),
            str(overrides.get('osl', cfg.osl)),
            str(overrides.get('isl_stdev', cfg.isl_stdev or 0)),
            str(overrides.get('osl_stdev', cfg.osl_stdev or 0)),
            str(overrides.get('mode', self._dataset_mode())),
            str(overrides.get('hit_pct', cfg.prefix_cache_hit_pct or 0)),
            str(overrides.get('cache_mode', cfg.prefix_cache_mode or 'identical')),
            str(overrides.get('groups', cfg.prefix_cache_groups or 0)),
            str(overrides.get('turns', getattr(cfg, 'turns', 1) or 1)),
            str(overrides.get('first_prompt_tokens', getattr(cfg, 'first_prompt_tokens', 0) or 0)),
            str(overrides.get('prefix_tokens', getattr(cfg, 'prefix_tokens', 0) or 0)),
            str(overrides.get('prefix_count', getattr(cfg, 'prefix_count', 1) or 1)),
            str(overrides.get('use_corpus', getattr(cfg, 'use_corpus', False))),
        ]
        seed_input = ':'.join(parts)
        return int(hashlib.sha256(seed_input.encode()).hexdigest()[:8], 16)

    def _save_seed_config(self, seed, config_dict):
        """Save seed → config mapping to database for reproduction."""
        if not self.db_manager:
            return
        try:
            import json
            from datetime import datetime
            with self.db_manager.get_connection() as conn:
                conn.execute(
                    'INSERT OR REPLACE INTO dataset_seeds (seed, config_json, created_at) VALUES (?, ?, ?)',
                    (seed, json.dumps(config_dict), datetime.now().isoformat())
                )
        except Exception as e:
            self.log(f"   Warning: failed to save seed config: {e}", 'warning')

    def _generate_random_dataset(self):
        """Generate a dataset with unique random prompts on the workload pod.

        Runs generate_dataset script on the workload pod via kubectl exec.
        The dataset is written directly to the workload pod's shared storage.
        """
        isl = self.config.isl
        osl = self.config.osl
        mode = self._dataset_mode()
        seed = getattr(self.config, 'prefix_cache_seed', None)
        if not seed:
            seed = self._compute_dataset_seed(mode=mode)

        max_reqs = getattr(self.config, 'max_requests', None) or int(getattr(self.config, 'qps', 100) * getattr(self.config, 'test_duration', 300))
        pool_size = max(max_reqs * 3, 100)  # 3× max_requests for cache churn

        dataset_path = f'/mnt/storage/prefix-cache-datasets/{mode}-workload-{isl}-{osl}-{seed}.jsonl'

        # Ensure workload pod is running
        self.orchestrator.ensure_guidellm_pod(self.config, log_callback=lambda msg: self.log(msg, 'info'))

        kubectl = self.orchestrator.deployment_manager.kubectl
        pod_name = self.orchestrator._guidellm_pod_name
        exists = kubectl.run(
            ['exec', pod_name, '-n', self.config.namespace, '--',
             'test', '-f', dataset_path], check=False
        ).returncode == 0

        if exists:
            self.log(f"   Reusing existing random dataset on workload pod: {os.path.basename(dataset_path)}", 'info')
        else:
            corpus_note = ' (real text corpus)' if mode == 'corpus' else ''
            self.log(f"Generating {mode} dataset on workload pod: {pool_size} rows, ISL={isl}, OSL={osl}{corpus_note}", 'info')
            isl_stdev = self.config.isl_stdev or 0
            osl_stdev = self.config.osl_stdev or 0
            cmd = (
                f'generate_dataset'
                f' --model "{self.config.model_name}"'
                f' --isl {isl} --osl {osl}'
                f' --seed {seed} --rows {pool_size}'
                f' --output {dataset_path}'
                f' --mode {mode}'
            )
            if isl_stdev > 0:
                cmd += f' --isl-stdev {isl_stdev}'
            if osl_stdev > 0:
                cmd += f' --osl-stdev {osl_stdev}'

            result = kubectl.run(
                ['exec', pod_name, '-n', self.config.namespace, '--', 'bash', '-c', cmd],
                check=False, timeout=3600
            )
            if result.returncode != 0:
                self.log(f"   ❌ Dataset generation failed: {result.stderr[:200]}", 'error')
                raise RuntimeError("Failed to generate random dataset on workload pod")
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    self.log(f"   {line}", 'info')

        self.random_dataset_path = dataset_path
        self._save_seed_config(seed, {
            'type': 'single_turn', 'model': self.config.model_name,
            'isl': isl, 'osl': osl, 'isl_stdev': isl_stdev, 'osl_stdev': osl_stdev,
            'mode': mode, 'rows': pool_size, 'use_corpus': getattr(self.config, 'use_corpus', False),
        })
        return dataset_path

    def _generate_turn_dataset(self):
        """Generate a multi-turn conversation dataset using guidellm's SyntheticTextDataset.

        Runs generate_turn_dataset script on the workload pod. The dataset uses
        guidellm's native conversation format (conversation_turns JSONL) and supports
        first_prompt_tokens, prefix_buckets, and all distribution parameters.
        """
        cfg = self.config
        seed = self._compute_dataset_seed(turns=cfg.turns)

        max_reqs = getattr(cfg, 'max_requests', None) or int(getattr(cfg, 'qps', 100) * getattr(cfg, 'test_duration', 300))
        rows = max(max_reqs * 2, 100)

        dataset_path = f'/mnt/storage/prefix-cache-datasets/turn-workload-{cfg.isl}-{cfg.osl}-t{cfg.turns}-{seed}.jsonl'

        self.orchestrator.ensure_guidellm_pod(cfg, log_callback=lambda msg: self.log(msg, 'info'))
        kubectl = self.orchestrator.deployment_manager.kubectl
        pod_name = self.orchestrator._guidellm_pod_name

        exists = kubectl.run(
            ['exec', pod_name, '-n', cfg.namespace, '--',
             'test', '-f', dataset_path], check=False
        ).returncode == 0

        if exists:
            self.log(f"   Reusing existing turn dataset: {os.path.basename(dataset_path)}", 'info')
        else:
            self.log(f"Generating turn dataset: {rows} conversations, {cfg.turns} turns each", 'info')
            cmd = (
                f'generate_turn_dataset'
                f' --model "{cfg.model_name}"'
                f' --prompt-tokens {cfg.isl} --output-tokens {cfg.osl}'
                f' --turns {cfg.turns}'
                f' --rows {rows} --seed {seed}'
                f' --output {dataset_path}'
            )
            if cfg.isl_stdev:
                cmd += f' --prompt-tokens-stdev {cfg.isl_stdev}'
            if getattr(cfg, 'isl_min', None):
                cmd += f' --prompt-tokens-min {cfg.isl_min}'
            if getattr(cfg, 'isl_max', None):
                cmd += f' --prompt-tokens-max {cfg.isl_max}'
            if cfg.osl_stdev:
                cmd += f' --output-tokens-stdev {cfg.osl_stdev}'
            if getattr(cfg, 'osl_min', None):
                cmd += f' --output-tokens-min {cfg.osl_min}'
            if getattr(cfg, 'osl_max', None):
                cmd += f' --output-tokens-max {cfg.osl_max}'
            if getattr(cfg, 'first_prompt_tokens', None):
                cmd += f' --first-prompt-tokens {cfg.first_prompt_tokens}'
            if getattr(cfg, 'first_prompt_tokens_stdev', None):
                cmd += f' --first-prompt-tokens-stdev {cfg.first_prompt_tokens_stdev}'
            if getattr(cfg, 'first_prompt_tokens_min', None):
                cmd += f' --first-prompt-tokens-min {cfg.first_prompt_tokens_min}'
            if getattr(cfg, 'first_prompt_tokens_max', None):
                cmd += f' --first-prompt-tokens-max {cfg.first_prompt_tokens_max}'
            if getattr(cfg, 'first_output_tokens', None):
                cmd += f' --first-output-tokens {cfg.first_output_tokens}'
            if getattr(cfg, 'first_output_tokens_stdev', None):
                cmd += f' --first-output-tokens-stdev {cfg.first_output_tokens_stdev}'
            if getattr(cfg, 'prefix_tokens', None):
                cmd += f' --prefix-tokens {cfg.prefix_tokens}'
            if getattr(cfg, 'prefix_count', None):
                cmd += f' --prefix-count {cfg.prefix_count}'
            if getattr(cfg, 'use_corpus', False):
                cmd += ' --use-corpus'

            result = kubectl.run(
                ['exec', pod_name, '-n', cfg.namespace, '--', 'bash', '-c', cmd],
                check=False, timeout=7200
            )
            if result.returncode != 0:
                self.log(f"   ❌ Turn dataset generation failed: {result.stderr[:200]}", 'error')
                raise RuntimeError("Failed to generate turn dataset on workload pod")
            if result.stderr:
                for line in result.stderr.strip().splitlines()[-5:]:
                    self.log(f"   {line}", 'info')

        self.turn_dataset_path = dataset_path
        self._save_seed_config(seed, {
            'type': 'multi_turn', 'model': cfg.model_name,
            'prompt_tokens': cfg.isl, 'output_tokens': cfg.osl,
            'prompt_tokens_stdev': cfg.isl_stdev, 'output_tokens_stdev': cfg.osl_stdev,
            'turns': cfg.turns, 'rows': rows,
            'first_prompt_tokens': getattr(cfg, 'first_prompt_tokens', None),
            'first_prompt_tokens_stdev': getattr(cfg, 'first_prompt_tokens_stdev', None),
            'first_prompt_tokens_min': getattr(cfg, 'first_prompt_tokens_min', None),
            'first_prompt_tokens_max': getattr(cfg, 'first_prompt_tokens_max', None),
            'prefix_tokens': getattr(cfg, 'prefix_tokens', 0),
            'prefix_count': getattr(cfg, 'prefix_count', 1),
            'use_corpus': getattr(cfg, 'use_corpus', False),
        })
        return dataset_path

    def _generate_calibration_dataset(self, isl: int, osl: int, label: str = 'calibration', pool_size: int = 0):
        """Generate a dataset for calibration tests.

        Pre-generates prompts so guidellm doesn't regenerate synthetic
        tokens for every test in the sweep. Especially important for
        long-context prefill (ISL=100K) where synthetic generation is slow.
        """
        seed = self._compute_dataset_seed(isl=isl, osl=osl, mode='calibration')

        pool_size = max(pool_size, 10)
        dataset_path = f'/mnt/storage/prefix-cache-datasets/calibration-{label}-{isl}-{osl}-{seed}.jsonl'

        try:
            self.orchestrator.ensure_guidellm_pod(self.config, log_callback=lambda msg: self.log(msg, 'info'))
            kubectl = self.orchestrator.deployment_manager.kubectl
            pod_name = self.orchestrator._guidellm_pod_name

            exists = kubectl.run(
                ['exec', pod_name, '-n', self.config.namespace, '--',
                 'test', '-f', dataset_path], check=False
            ).returncode == 0

            if exists:
                self.log(f"   Reusing calibration dataset ({label}): {os.path.basename(dataset_path)}", 'info')
            else:
                mode = self._dataset_mode()
                self.log(f"   Generating calibration dataset ({label}, {mode}): {pool_size} rows, ISL={isl}, OSL={osl}", 'info')
                cmd = (
                    f'generate_dataset'
                    f' --model "{self.config.model_name}"'
                    f' --isl {isl} --osl {osl}'
                    f' --seed {seed} --rows {pool_size}'
                    f' --output {dataset_path}'
                    f' --mode {mode}'
                )
                result = kubectl.run(
                    ['exec', pod_name, '-n', self.config.namespace, '--', 'bash', '-c', cmd],
                    check=False, timeout=3600
                )
                if result.returncode != 0:
                    self.log("   ⚠️  Calibration dataset generation failed, falling back to synthetic", 'warning')
                    return None

            return dataset_path
        except Exception as e:
            self.log(f"   ⚠️  Calibration dataset generation failed ({e}), falling back to synthetic", 'warning')
            return None

    def _generate_prefix_cache_dataset(self):
        """Generate a prefix cache dataset on the workload pod.

        For single-turn: uses the fast generate_dataset script.
        For multi-turn: delegates to generate_turn_dataset with prefix_buckets
        mapped from the prefix cache mode settings.
        """
        hit_pct = self.config.prefix_cache_hit_pct
        isl = self.config.isl
        osl = self.config.osl
        is_multi_turn = getattr(self.config, 'turns', 1) > 1

        # Multi-turn with prefix cache: map prefix cache modes to prefix_buckets
        # and delegate to _generate_turn_dataset
        if is_multi_turn:
            cache_mode = self.config.prefix_cache_mode or 'identical'
            prefix_tokens = int(isl * hit_pct / 100)
            if cache_mode == 'identical':
                self.config.prefix_tokens = prefix_tokens
                self.config.prefix_count = 1
            elif cache_mode == 'shared_prefix':
                self.config.prefix_tokens = prefix_tokens
                self.config.prefix_count = 1
            elif cache_mode == 'multi_group':
                groups = int(self.config.prefix_cache_groups or 5)
                self.config.prefix_tokens = prefix_tokens
                self.config.prefix_count = groups
            self.log(f"   Multi-turn prefix cache: {cache_mode}, {hit_pct}% → prefix_tokens={prefix_tokens}, prefix_count={self.config.prefix_count}", 'info')
            self._generate_turn_dataset()
            self.config.workload_mode = 'dataset'
            self.config.dataset_source = self.turn_dataset_path
            self.config.dataset_column = 'conversation_turns'
            self.config.dataset_max_output = osl
            self.log("   Workload switched to multi-turn dataset with prefix cache", 'info')
            return

        cache_mode = self.config.prefix_cache_mode or 'identical'
        groups_str = str(self.config.prefix_cache_groups or 5) if cache_mode == 'multi_group' else '0'
        if self.config.prefix_cache_seed is not None:
            seed = self.config.prefix_cache_seed
        else:
            seed = self._compute_dataset_seed(hit_pct=hit_pct, cache_mode=cache_mode, groups=int(groups_str))
            self.config.prefix_cache_seed = seed

        gpu_vram_gb = getattr(self, '_gpu_vram_gb', 80.0)
        total_gpus = self.config.total_gpus
        model_size_gb = self._estimate_model_size_gb()
        available_cache_gb = max(1, total_gpus * gpu_vram_gb * 0.9 - model_size_gb)
        cacheable_tokens = available_cache_gb * 1024 * 1024 * 1024 / 512
        cacheable_sequences = max(100, int(cacheable_tokens / isl))
        pool_size = max(1000, int(cacheable_sequences * 1.5))
        min_rows = int(getattr(self.config, 'qps', 100) * getattr(self.config, 'test_duration', 300) * 1.5)
        pool_size = max(pool_size, min_rows)
        if getattr(self.config, 'use_corpus', False):
            pool_size = min(pool_size, min_rows)  # corpus: cap at actual need, no excess
        else:
            pool_size = min(pool_size, 100000)

        dataset_path = f'/mnt/storage/prefix-cache-datasets/prefix-cache-{cache_mode}-{seed}.jsonl'

        corpus_label = ' (real text corpus)' if getattr(self.config, 'use_corpus', False) else ''
        self.log(f"Generating prefix cache dataset: {hit_pct}% hit ratio, {pool_size} rows, seed={seed}{corpus_label}", 'info')
        self.log(f"   Estimated cacheable sequences: {cacheable_sequences}", 'info')
        self.log(f"   Mode: {cache_mode}", 'info')

        # Ensure workload pod is running
        self.orchestrator.ensure_guidellm_pod(self.config, log_callback=lambda msg: self.log(msg, 'info'))

        kubectl = self.orchestrator.deployment_manager.kubectl
        pod_name = self.orchestrator._guidellm_pod_name
        exists = kubectl.run(
            ['exec', pod_name, '-n', self.config.namespace, '--',
             'test', '-f', dataset_path], check=False
        ).returncode == 0

        isl_stdev = self.config.isl_stdev or 0
        osl_stdev = self.config.osl_stdev or 0
        groups = int(self.config.prefix_cache_groups or 5) if cache_mode == 'multi_group' else 0

        if exists:
            self.log(f"   Reusing existing dataset: {os.path.basename(dataset_path)}", 'info')
        else:

            structured_prefix = getattr(self.config, 'structured_prefix', False)
            if structured_prefix and hit_pct > 0:
                prefix_groups = groups if groups > 0 else 5
                max_isl = isl + (int(isl_stdev) if isl_stdev > 0 else 0)
                cmd = (
                    f'generate_dataset'
                    f' --model "{self.config.model_name}"'
                    f' --isl {isl} --osl {osl}'
                    f' --seed {seed} --rows {pool_size}'
                    f' --output {dataset_path}'
                    f' --mode prefix_group'
                    f' --hit-pct {hit_pct}'
                    f' --prefix-groups {prefix_groups}'
                )
                self.log(f"   Structured prefix mode: {prefix_groups} groups, {hit_pct}% prefix per row (ISL {isl}-{max_isl})", 'info')
            else:
                cmd = (
                    f'generate_dataset'
                    f' --model "{self.config.model_name}"'
                    f' --isl {isl} --osl {osl}'
                    f' --seed {seed} --rows {pool_size}'
                    f' --output {dataset_path}'
                    f' --mode cache --hit-pct {hit_pct}'
                )
            if isl_stdev > 0:
                cmd += f' --isl-stdev {isl_stdev}'
            if osl_stdev > 0:
                cmd += f' --osl-stdev {osl_stdev}'
            if getattr(self.config, 'use_corpus', False):
                cmd += ' --use-corpus'

            result = kubectl.run(
                ['exec', pod_name, '-n', self.config.namespace, '--', 'bash', '-c', cmd],
                check=False, timeout=3600
            )
            if result.returncode != 0:
                self.log(f"   ❌ Dataset generation failed: {result.stderr[:200]}", 'error')
                raise RuntimeError("Failed to generate prefix cache dataset on workload pod")
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    self.log(f"   {line}", 'info')

        self.config.workload_mode = 'dataset'
        self.config.dataset_source = dataset_path
        self.config.dataset_column = 'prompt'
        self.config.dataset_max_output = osl
        self.log("   Workload switched to dataset mode for prefix cache simulation", 'info')

        self._save_seed_config(seed, {
            'type': 'single_turn', 'model': self.config.model_name,
            'isl': isl, 'osl': osl, 'isl_stdev': isl_stdev, 'osl_stdev': osl_stdev,
            'mode': 'prefix_group' if (structured_prefix and hit_pct > 0) else 'cache',
            'hit_pct': hit_pct, 'prefix_groups': groups if cache_mode == 'multi_group' else 0,
            'rows': pool_size, 'use_corpus': getattr(self.config, 'use_corpus', False),
        })

        # Persist seed to DB so resume regenerates the same dataset
        if self.run_id and self.db_manager:
            try:
                with self.db_manager.get_connection() as conn:
                    conn.execute(
                        'UPDATE optimization_runs SET prefix_cache_seed = ? WHERE id = ?',
                        (seed, self.run_id)
                    )
            except Exception as e:
                self.log(f"   Warning: failed to persist seed to DB: {e}", 'warning')
