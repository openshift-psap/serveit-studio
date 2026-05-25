"""Prefix cache dataset generation."""

import os
import hashlib
import random
from pathlib import Path
from typing import Optional


class DatasetMixin:
    """Mixin providing dataset generation methods for RecipeOptimizer."""

    def _generate_prefix_cache_dataset(self):
        """Generate a synthetic dataset with controlled prefix cache hit ratio.

        Creates a .jsonl file where prefix_cache_hit_pct% of rows share an
        identical prompt (guaranteeing prefix cache hits) and the rest are
        unique random prompts. The dataset is sized to overflow GPU prefix
        cache so unique prompts don't accidentally get cached.
        """
        import hashlib
        import json as _json
        import random
        from pathlib import Path

        hit_pct = self.config.prefix_cache_hit_pct
        isl = self.config.isl
        osl = self.config.osl

        # Compute deterministic seed from config (includes stdev so different variation = different dataset)
        cache_mode = self.config.prefix_cache_mode or 'identical'
        groups_str = str(self.config.prefix_cache_groups or 5) if cache_mode == 'multi_group' else '0'
        seed_input = f"{self.config.model_name}:{isl}:{osl}:{hit_pct}:{self.config.isl_stdev or 0}:{self.config.osl_stdev or 0}:{cache_mode}:{groups_str}"
        if self.config.prefix_cache_seed is not None:
            seed = self.config.prefix_cache_seed
        else:
            seed = int(hashlib.sha256(seed_input.encode()).hexdigest()[:8], 16)
            self.config.prefix_cache_seed = seed

        # Calculate pool size: overflow the prefix cache
        gpu_vram_gb = getattr(self, '_gpu_vram_gb', 80.0)
        total_gpus = self.config.total_gpus
        model_size_gb = self._estimate_model_size_gb()
        available_cache_gb = max(1, total_gpus * gpu_vram_gb * 0.9 - model_size_gb)
        # KV cache per token ≈ 2 * num_layers * num_kv_heads * head_dim * dtype_bytes
        # Simplified: ~0.5KB/token for typical models
        cacheable_tokens = available_cache_gb * 1024 * 1024 * 1024 / 512
        cacheable_sequences = max(100, int(cacheable_tokens / isl))
        # Pool needs enough unique rows that they get evicted before cycling back.
        # 1.5x the cacheable count is sufficient — the duplicates fill up the cache,
        # and unique rows rotate through faster than the cache can hold them all.
        pool_size = max(1000, int(cacheable_sequences * 1.5))
        pool_size = min(pool_size, 10000)  # Cap at 10K to keep file size reasonable

        self.log(f"Generating prefix cache dataset: {hit_pct}% hit ratio, {pool_size} rows, seed={seed}", 'info')
        self.log(f"   Estimated cacheable sequences: {cacheable_sequences}", 'info')

        rng = random.Random(seed)

        # Build vocabulary of printable words for prompt generation
        # Use model tokenizer if available, otherwise generate random words
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
            vocab = None

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

        # Generate the shared prompt (fixed length — must be identical for cache hits)
        shared_rng = random.Random(seed)
        shared_prompt = make_prompt(isl, shared_rng)

        isl_stdev = self.config.isl_stdev or 0
        osl_stdev = self.config.osl_stdev or 0

        cache_mode = self.config.prefix_cache_mode or 'identical'
        self.log(f"   Mode: {cache_mode}", 'info')

        # Generate dataset
        output_dir = Path(os.environ.get('HOME_STORAGE_DIR', '/mnt/storage')) / 'prefix-cache-datasets'
        output_dir.mkdir(parents=True, exist_ok=True)
        dataset_path = output_dir / f'prefix-cache-{cache_mode}-{seed}.jsonl'

        if dataset_path.exists():
            self.log(f"   Reusing existing dataset: {dataset_path}", 'info')
        else:
            rows = []

            if cache_mode == 'shared_prefix':
                shared_token_count = int(isl * hit_pct / 100)
                unique_token_count = isl - shared_token_count
                shared_prefix_text = make_prompt(shared_token_count, random.Random(seed))
                self.log(f"   Shared prefix: {shared_token_count} tokens, unique suffix: {unique_token_count} tokens", 'info')

                for i in range(pool_size):
                    suffix_rng = random.Random(seed + i + 1)
                    if isl_stdev > 0:
                        row_unique = max(1, int(suffix_rng.gauss(unique_token_count, isl_stdev * unique_token_count / isl)))
                    else:
                        row_unique = unique_token_count
                    suffix_text = make_prompt(row_unique, suffix_rng)
                    prompt = shared_prefix_text + ' ' + suffix_text
                    if osl_stdev > 0:
                        row_osl = max(1, int(suffix_rng.gauss(osl, osl_stdev)))
                    else:
                        row_osl = osl
                    rows.append({"prompt": prompt, "output_tokens_count": row_osl})

            elif cache_mode == 'multi_group':
                num_groups = max(2, self.config.prefix_cache_groups or 5)
                num_grouped = int(pool_size * hit_pct / 100)
                num_unique = pool_size - num_grouped
                per_group = max(1, num_grouped // num_groups)
                self.log(f"   Multi-group: {num_groups} groups, {per_group} requests/group, {num_unique} unique", 'info')

                group_prompts = []
                for g in range(num_groups):
                    group_rng = random.Random(seed + 100000 + g)
                    group_prompts.append(make_prompt(isl, group_rng))

                for g in range(num_groups):
                    for _ in range(per_group):
                        rows.append({"prompt": group_prompts[g], "output_tokens_count": osl})

                for i in range(num_unique):
                    unique_rng = random.Random(seed + i + 1)
                    if isl_stdev > 0:
                        row_isl = max(16, int(unique_rng.gauss(isl, isl_stdev)))
                    else:
                        row_isl = isl
                    if osl_stdev > 0:
                        row_osl = max(1, int(unique_rng.gauss(osl, osl_stdev)))
                    else:
                        row_osl = osl
                    rows.append({"prompt": make_prompt(row_isl, unique_rng), "output_tokens_count": row_osl})

            else:
                num_shared = int(pool_size * hit_pct / 100)
                num_unique = pool_size - num_shared
                for _ in range(num_shared):
                    rows.append({"prompt": shared_prompt, "output_tokens_count": osl})
                for i in range(num_unique):
                    unique_rng = random.Random(seed + i + 1)
                    if isl_stdev > 0:
                        row_isl = max(16, int(unique_rng.gauss(isl, isl_stdev)))
                    else:
                        row_isl = isl
                    if osl_stdev > 0:
                        row_osl = max(1, int(unique_rng.gauss(osl, osl_stdev)))
                    else:
                        row_osl = osl
                    rows.append({"prompt": make_prompt(row_isl, unique_rng), "output_tokens_count": row_osl})

            rng.shuffle(rows)

            with open(dataset_path, 'w') as f:
                for row in rows:
                    f.write(_json.dumps(row) + '\n')

            file_size_mb = dataset_path.stat().st_size / (1024 * 1024)
            self.log(f"   Generated {dataset_path} ({file_size_mb:.1f} MB)", 'success')

        # Switch workload mode to dataset for all subsequent tests
        self.config.workload_mode = 'dataset'
        self.config.dataset_source = str(dataset_path)
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
