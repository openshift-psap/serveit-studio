"""Dataset generation for optimization workloads."""

import os
import json as _json
import hashlib
import random
from pathlib import Path
from typing import Optional


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

    def _generate_random_dataset(self):
        """Generate a dataset with unique random prompts on the workload pod.

        Runs generate_dataset script on the workload pod via kubectl exec.
        The dataset is written directly to the workload pod's shared storage.
        """
        isl = self.config.isl
        osl = self.config.osl
        seed = getattr(self.config, 'prefix_cache_seed', None)
        if not seed:
            seed_input = f"{self.config.model_name}:{isl}:{osl}:random"
            seed = int(hashlib.md5(seed_input.encode()).hexdigest()[:8], 16)

        pool_size = int(getattr(self.config, 'qps', 100) * getattr(self.config, 'test_duration', 300) * 1.5)
        pool_size = max(1000, min(pool_size, 100000))

        dataset_path = f'/mnt/storage/prefix-cache-datasets/random-workload-{isl}-{osl}-{seed}.jsonl'

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
            self.log(f"Generating random dataset on workload pod: {pool_size} rows, ISL={isl}, OSL={osl}", 'info')
            isl_stdev = self.config.isl_stdev or 0
            osl_stdev = self.config.osl_stdev or 0
            cmd = (
                f'/mnt/storage/generate_dataset'
                f' --model "{self.config.model_name}"'
                f' --isl {isl} --osl {osl}'
                f' --seed {seed} --rows {pool_size}'
                f' --output {dataset_path}'
                f' --mode random'
            )
            if isl_stdev > 0:
                cmd += f' --isl-stdev {isl_stdev}'
            if osl_stdev > 0:
                cmd += f' --osl-stdev {osl_stdev}'

            result = kubectl.run(
                ['exec', pod_name, '-n', self.config.namespace, '--', 'bash', '-c', cmd],
                check=False
            )
            if result.returncode != 0:
                self.log(f"   ❌ Dataset generation failed: {result.stderr[:200]}", 'error')
                raise RuntimeError(f"Failed to generate random dataset on workload pod")
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    self.log(f"   {line}", 'info')

        self.random_dataset_path = dataset_path
        return dataset_path

    def _generate_prefix_cache_dataset(self):
        """Generate a prefix cache dataset on the workload pod.

        Uses the generate_dataset script on the workload pod for speed.
        """
        hit_pct = self.config.prefix_cache_hit_pct
        isl = self.config.isl
        osl = self.config.osl

        cache_mode = self.config.prefix_cache_mode or 'identical'
        groups_str = str(self.config.prefix_cache_groups or 5) if cache_mode == 'multi_group' else '0'
        seed_input = f"{self.config.model_name}:{isl}:{osl}:{hit_pct}:{self.config.isl_stdev or 0}:{self.config.osl_stdev or 0}:{cache_mode}:{groups_str}"
        if self.config.prefix_cache_seed is not None:
            seed = self.config.prefix_cache_seed
        else:
            seed = int(hashlib.sha256(seed_input.encode()).hexdigest()[:8], 16)
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
        pool_size = min(pool_size, 100000)

        dataset_path = f'/mnt/storage/prefix-cache-datasets/prefix-cache-{cache_mode}-{seed}.jsonl'

        self.log(f"Generating prefix cache dataset: {hit_pct}% hit ratio, {pool_size} rows, seed={seed}", 'info')
        self.log(f"   Estimated cacheable sequences: {cacheable_sequences}", 'info')
        self.log(f"   Mode: {cache_mode}", 'info')

        # Ensure workload pod is running
        from core.optimizer.config import TestConfig
        dummy_config = TestConfig(model_name=self.config.model_name, namespace=self.config.namespace,
                                  pvc_name=getattr(self.config, 'pvc_name', 'serveit-cache'))
        self.orchestrator.ensure_guidellm_pod(dummy_config, log_callback=lambda msg: self.log(msg, 'info'))

        kubectl = self.orchestrator.deployment_manager.kubectl
        pod_name = self.orchestrator._guidellm_pod_name
        exists = kubectl.run(
            ['exec', pod_name, '-n', self.config.namespace, '--',
             'test', '-f', dataset_path], check=False
        ).returncode == 0

        if exists:
            self.log(f"   Reusing existing dataset: {os.path.basename(dataset_path)}", 'info')
        else:
            isl_stdev = self.config.isl_stdev or 0
            osl_stdev = self.config.osl_stdev or 0
            cmd = (
                f'/mnt/storage/generate_dataset'
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

            result = kubectl.run(
                ['exec', pod_name, '-n', self.config.namespace, '--', 'bash', '-c', cmd],
                check=False
            )
            if result.returncode != 0:
                self.log(f"   ❌ Dataset generation failed: {result.stderr[:200]}", 'error')
                raise RuntimeError(f"Failed to generate prefix cache dataset on workload pod")
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    self.log(f"   {line}", 'info')

        self.config.workload_mode = 'dataset'
        self.config.dataset_source = dataset_path
        self.config.dataset_column = 'prompt'
        self.config.dataset_max_output = osl
        self.log(f"   Workload switched to dataset mode for prefix cache simulation", 'info')

        # Persist seed to DB so resume regenerates the same dataset
        if self.run_id and self.db_manager:
            try:
                with self.db_manager.get_connection() as conn:
                    conn.execute(
                        'UPDATE optimization_runs SET prefix_cache_seed = ?, config_json = ? WHERE id = ?',
                        (seed, _json.dumps(self.config.to_dict()), self.run_id)
                    )
            except Exception as e:
                self.log(f"   Warning: failed to persist seed to DB: {e}", 'warning')
