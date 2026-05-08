"""
Inftune Studio Test Planner

Calculates resource requirements and plans test configurations based on:
- Model size and VRAM requirements
- Available GPU resources
- User's optimization goal
"""

import logging
import requests
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from core.utils import Architecture, next_power_of_2
from core.cloud_constraints import CloudProvider, CloudConstraints, validate_pd_config
from core.providers import ProviderRegistry
from core.web_deployer import NetworkIntegrator
from core.networking import NetworkType

logger = logging.getLogger(__name__)


def calculate_engine_memory_config(
    isl: int,
    osl: int,
    num_users: int,
    model_size_b: float = 8.0,
    dtype: str = 'fp16',
    gpu_vram_gb: float = 80.0,
    tensor_parallelism: int = 1,
    model_config: Optional[Dict] = None,
    model_name: Optional[str] = None,
    isl_stdev: Optional[int] = None,
    osl_stdev: Optional[int] = None
) -> Tuple[int, float]:
    """
    Calculate max_model_len and gpu_memory_utilization together.

    This ensures both values are coordinated to avoid OOM errors by considering:
    1. Sequence length (ISL + OSL), adjusted for stdev if provided
    2. Expected batch size (derived from num_users)
    3. KV cache memory requirements
    4. Model weights

    When isl_stdev/osl_stdev are provided (guidellm generates normally-distributed
    sequence lengths), max_model_len uses mean + 2*stdev to cover 97.7% of sequences.

    Args:
        isl: Input sequence length (mean when stdev is set)
        osl: Output sequence length (mean when stdev is set)
        num_users: Number of concurrent users (affects batch size)
        model_size_b: Model size in billions of parameters
        dtype: Data type (fp16, fp8, etc.)
        gpu_vram_gb: GPU VRAM capacity in GB
        tensor_parallelism: TP size (memory is divided across TP workers)
        model_config: Optional HuggingFace model config
        isl_stdev: Optional standard deviation for ISL distribution
        osl_stdev: Optional standard deviation for OSL distribution

    Returns:
        Tuple of (max_model_len, gpu_memory_utilization)

    Raises:
        ValueError: If ISL + OSL exceeds model's max context length
    """
    # 1. Load model config if model_name provided but config not
    if model_name and not model_config:
        try:
            from transformers import AutoConfig
            logger.info(f"Loading model config for validation: {model_name}")
            model_config = AutoConfig.from_pretrained(model_name, trust_remote_code=True).to_dict()
        except Exception as e:
            logger.warning(f"Could not load model config from {model_name}: {e}")
            logger.warning("Skipping model context length validation")

    # 2. Calculate stdev-adjusted max sequence lengths
    # When stdev is set, guidellm generates normally-distributed lengths.
    # Use mean + 3*stdev to cover 99.87% of the distribution on the high end.
    effective_isl = isl + 3 * isl_stdev if isl_stdev else isl
    effective_osl = osl + 3 * osl_stdev if osl_stdev else osl

    if isl_stdev or osl_stdev:
        logger.info(f"Stdev adjustment: ISL {isl}→{effective_isl} (stdev={isl_stdev}), "
                    f"OSL {osl}→{effective_osl} (stdev={osl_stdev})")

    # 3. Validate against model's absolute max context length
    CHAT_TEMPLATE_OVERHEAD = 200

    if model_config:
        model_max_position_embeddings = model_config.get('max_position_embeddings', 4096)

        total_tokens_needed = effective_isl + effective_osl + CHAT_TEMPLATE_OVERHEAD
        if total_tokens_needed > model_max_position_embeddings:
            raise ValueError(
                f"Workload exceeds model's maximum context length!\n"
                f"  Requested: ISL={effective_isl} + OSL={effective_osl} + overhead={CHAT_TEMPLATE_OVERHEAD} = {total_tokens_needed} tokens\n"
                f"  (ISL includes +2*stdev={isl_stdev or 0}, OSL includes +2*stdev={osl_stdev or 0})\n"
                f"  Model max: {model_max_position_embeddings} tokens (from max_position_embeddings)\n"
                f"  Exceeds by: {total_tokens_needed - model_max_position_embeddings} tokens\n"
                f"  Solution: Reduce ISL/OSL or stdev to fit within {model_max_position_embeddings - CHAT_TEMPLATE_OVERHEAD} tokens total"
            )

        logger.info(f"✓ Workload validation: {total_tokens_needed} tokens <= {model_max_position_embeddings} model max")

    # 4. Calculate max_model_len using stdev-adjusted values, capped at max_position_embeddings
    # 5% headroom accounts for chat template tokens and guidellm synthetic text
    max_model_len = int((effective_isl + effective_osl) * 1.05)
    if model_config:
        model_max = model_config.get('max_position_embeddings', 4096)
        max_model_len = min(max_model_len, model_max)

    # 5. Get model architecture parameters
    if model_config:
        num_layers = model_config.get('num_hidden_layers', 32)
        num_kv_heads = model_config.get('num_key_value_heads')
        if num_kv_heads is None:
            num_kv_heads = model_config.get('num_attention_heads', 32)
        hidden_size = model_config.get('hidden_size', 4096)
        num_attention_heads = model_config.get('num_attention_heads', 32)
        head_dim = hidden_size // num_attention_heads
    else:
        num_layers = 32
        num_kv_heads = 8
        head_dim = 128

    # 6. KV cache per sequence (always bf16 regardless of weight quantization)
    kv_heads_per_gpu = max(num_kv_heads // tensor_parallelism, 1)
    kv_dtype_bytes = 2
    kv_cache_per_seq_gb = (2 * num_layers * kv_heads_per_gpu * head_dim * max_model_len * kv_dtype_bytes) / (1024**3)

    # 7. Estimated concurrent sequences from num_users
    if num_users >= 200:
        estimated_batch_size = min(num_users // 5, 128)
    elif num_users >= 50:
        estimated_batch_size = min(num_users // 4, 64)
    else:
        estimated_batch_size = min(num_users // 2, 32)
    estimated_batch_size = max(estimated_batch_size, 8)

    kv_cache_for_batch_gb = estimated_batch_size * kv_cache_per_seq_gb

    # 8. gpu_memory_utilization: give vLLM maximum safe allocation
    #
    # vLLM internally handles the breakdown: it loads the model, warms up CUDA graphs
    # and torch.compile, then allocates KV cache blocks with whatever memory remains.
    # The runtime overhead (CUDA graphs, activation buffers, quantization dequant buffers,
    # NIXL connector, torch.compile cache) varies dramatically by model architecture,
    # quantization method, and vLLM version — it can't be reliably predicted externally.
    #
    # Our job is just to tell vLLM how much of the GPU it can use.
    # Reserve only what the OS/CUDA driver needs (constant ~5 GiB).
    os_reserve_gb = max(gpu_vram_gb * 0.05, 5.0)
    gpu_memory_utilization = (gpu_vram_gb - os_reserve_gb) / gpu_vram_gb

    # 9. Log computed values
    allocated_vram_gb = gpu_vram_gb * gpu_memory_utilization

    logger.info("Engine memory configuration:")
    if isl_stdev or osl_stdev:
        logger.info(f"  max_model_len: {max_model_len} (ISL={isl}+2×{isl_stdev or 0}={effective_isl} + OSL={osl}+2×{osl_stdev or 0}={effective_osl})")
    else:
        logger.info(f"  max_model_len: {max_model_len} (ISL={isl} + OSL={osl})")
    logger.info(f"  TP={tensor_parallelism}, Users={num_users}")
    logger.info(f"  KV cache/seq ({max_model_len} tokens): {kv_cache_per_seq_gb:.3f} GB")
    logger.info(f"  KV for {estimated_batch_size} concurrent seqs: {kv_cache_for_batch_gb:.1f} GB")
    logger.info(f"  GPU: {gpu_vram_gb:.0f}GB → allocated {allocated_vram_gb:.1f}GB (OS reserve: {os_reserve_gb:.0f}GB)")
    logger.info(f"  gpu_memory_utilization: {gpu_memory_utilization:.4f}")

    return max_model_len, gpu_memory_utilization


# Architecture lookup table for common models (for offline/air-gapped environments)
MODEL_ARCHITECTURE_DB = {
    # Llama 3 family
    'llama-3-8b': {'layers': 32, 'kv_heads': 8, 'head_dim': 128},
    'llama-3-70b': {'layers': 80, 'kv_heads': 8, 'head_dim': 128},
    'llama-3.1-8b': {'layers': 32, 'kv_heads': 8, 'head_dim': 128},
    'llama-3.1-70b': {'layers': 80, 'kv_heads': 8, 'head_dim': 128},
    'llama-3.1-405b': {'layers': 126, 'kv_heads': 8, 'head_dim': 128},
    'llama-3.2-1b': {'layers': 16, 'kv_heads': 8, 'head_dim': 64},
    'llama-3.2-3b': {'layers': 28, 'kv_heads': 8, 'head_dim': 128},
    'llama-3.3-70b': {'layers': 80, 'kv_heads': 8, 'head_dim': 128},

    # Llama 2 family
    'llama-2-7b': {'layers': 32, 'kv_heads': 32, 'head_dim': 128},
    'llama-2-13b': {'layers': 40, 'kv_heads': 40, 'head_dim': 128},
    'llama-2-70b': {'layers': 80, 'kv_heads': 8, 'head_dim': 128},

    # Qwen family
    'qwen2.5-0.5b': {'layers': 24, 'kv_heads': 2, 'head_dim': 64},
    'qwen2.5-1.5b': {'layers': 28, 'kv_heads': 2, 'head_dim': 64},
    'qwen2.5-3b': {'layers': 36, 'kv_heads': 2, 'head_dim': 128},
    'qwen2.5-7b': {'layers': 28, 'kv_heads': 4, 'head_dim': 128},
    'qwen2.5-14b': {'layers': 48, 'kv_heads': 4, 'head_dim': 128},
    'qwen2.5-32b': {'layers': 64, 'kv_heads': 8, 'head_dim': 128},
    'qwen2.5-72b': {'layers': 80, 'kv_heads': 8, 'head_dim': 128},

    # Mixtral MoE
    'mixtral-8x7b': {'layers': 32, 'kv_heads': 8, 'head_dim': 128},
    'mixtral-8x22b': {'layers': 56, 'kv_heads': 8, 'head_dim': 128},

    # Mistral family
    'mistral-7b': {'layers': 32, 'kv_heads': 8, 'head_dim': 128},
    'mistral-nemo-12b': {'layers': 40, 'kv_heads': 8, 'head_dim': 128},

    # DeepSeek family
    'deepseek-v2': {'layers': 60, 'kv_heads': 8, 'head_dim': 128},
    'deepseek-coder-33b': {'layers': 62, 'kv_heads': 8, 'head_dim': 128},

    # GLM family
    'glm-4-9b': {'layers': 40, 'kv_heads': 2, 'head_dim': 128},

    # Yi family
    'yi-6b': {'layers': 32, 'kv_heads': 4, 'head_dim': 128},
    'yi-34b': {'layers': 60, 'kv_heads': 8, 'head_dim': 128},
}


@dataclass
class ModelRequirements:
    """Model resource requirements."""
    model_name: str
    estimated_vram_gb: float  # VRAM needed per GPU
    min_gpus: int  # Minimum GPUs needed to load model
    min_tp: int  # Minimum tensor parallelism value
    recommended_tp_options: List[int]  # Recommended TP values for this model
    # Breakdown details for transparency
    model_size_b: float  # Model size in billions of parameters
    dtype: str  # Data type (fp16, fp8, etc.)
    total_vram_gb: float  # Total VRAM needed for full model
    gpu_vram_gb: float  # VRAM available per GPU
    # Dynamic overhead breakdown
    kv_cache_gb: float  # KV cache based on sequence length
    activations_gb: float  # Activation memory
    cuda_overhead_gb: float  # CUDA kernels and framework overhead
    isl: int  # Input sequence length used for calculation
    osl: int  # Output sequence length used for calculation
    gpu_memory_utilization: float  # Dynamic GPU memory utilization setting


@dataclass
class TestConfiguration:
    """Configuration for a single test."""
    test_name: str
    architecture: Architecture
    gpus_required: int
    tp: int
    prefill_pods: int  # For PD architecture
    decode_pods: int  # For PD architecture
    ep_pods: int  # For EP architecture
    description: str
    # Recipe methodology fields
    recipe_step: Optional[int] = None  # Which step (2, 3, 7)
    recipe_phase: Optional[str] = None  # 'decode_pareto', 'prefill_efficiency', 'pd_validation', 'pd_optimization', 'analytical_resource_sizing'
    workload_isl: Optional[int] = None  # Override ISL for this test
    workload_osl: Optional[int] = None  # Override OSL for this test
    adaptive_guided: bool = False  # Whether adaptive optimizer will select this test
    decode_tp: Optional[int] = None  # TP for decode pods (PD architecture only)


@dataclass
class TestPlan:
    """Complete test plan for optimization run."""
    model_name: str
    total_gpus_available: int
    max_gpus_to_use: int
    optimization_goal: str
    model_requirements: ModelRequirements
    tests: List[TestConfiguration]
    can_proceed: bool
    error_message: Optional[str] = None
    # Recipe methodology fields
    recipe_mode: bool = True  # Use recipe-based optimization
    step2_tests: Optional[List[TestConfiguration]] = None  # Decode Pareto sweep
    step3_tests: Optional[List[TestConfiguration]] = None  # Prefill efficiency sweep
    step7_tests: Optional[List[TestConfiguration]] = None  # P/D validation sweep
    estimated_total_tests: Optional[str] = None  # "11-17 tests"
    # Cloud provider filtering
    cloud_filtered_count: int = 0  # Number of tests filtered by cloud constraints
    cloud_filter_reason: Optional[str] = None  # Reason for filtering
    cloud_provider: Optional[str] = None  # Cloud provider name


class TestPlanner:
    """Plans test configurations based on model requirements and available resources."""

    def __init__(self):
        """Initialize test planner."""
        pass

    def fetch_model_config(self, model_name: str, hf_token: Optional[str] = None, max_retries: int = 3) -> Optional[Dict]:
        """
        Fetch model config from HuggingFace with retry logic.

        Args:
            model_name: HuggingFace model name
            hf_token: Optional HuggingFace token for private/gated models
            max_retries: Maximum number of retry attempts (default: 3)

        Returns:
            Model config dict, or None if fetch fails
        """
        import time

        for attempt in range(max_retries):
            try:
                # Try to fetch config.json from HuggingFace
                url = f"https://huggingface.co/{model_name}/raw/main/config.json"

                # Add Authorization header if token provided
                headers = {}
                if hf_token:
                    headers['Authorization'] = f'Bearer {hf_token}'

                response = requests.get(url, headers=headers, timeout=10)

                if response.status_code == 200:
                    # Parse JSON with recursion limit safety
                    try:
                        config = response.json()
                    except (ValueError, RecursionError) as json_err:
                        error_type = type(json_err).__name__
                        logger.warning(f"Attempt {attempt + 1}/{max_retries}: Failed to parse config JSON: {error_type}")
                        if isinstance(json_err, RecursionError) and attempt < max_retries - 1:
                            # Retry on recursion errors
                            time.sleep(0.5 * (attempt + 1))  # Exponential backoff
                            continue
                        self._config_fetch_error = "json_parse_error"
                        return None

                    logger.info(f"Successfully fetched config for {model_name}")
                    self._config_fetch_error = None  # Clear any previous error
                    return config

                elif response.status_code == 401 or response.status_code == 403:
                    # Auth errors - don't retry
                    error_msg = f"Authentication required (status {response.status_code}) - private model?"
                    logger.warning(error_msg)
                    self._config_fetch_error = "auth"
                    return None

                elif response.status_code == 404:
                    # Not found - don't retry
                    error_msg = "Model or config not found (status 404)"
                    logger.warning(error_msg)
                    self._config_fetch_error = "not_found"
                    return None

                else:
                    error_msg = f"Attempt {attempt + 1}/{max_retries}: HTTP {response.status_code}"
                    logger.warning(error_msg)
                    if attempt < max_retries - 1:
                        time.sleep(0.5 * (attempt + 1))  # Exponential backoff
                        continue
                    self._config_fetch_error = f"http_{response.status_code}"
                    return None

            except requests.exceptions.Timeout:
                logger.warning(f"Attempt {attempt + 1}/{max_retries}: Timeout (>10s)")
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))
                    continue
                self._config_fetch_error = "timeout"
                return None

            except requests.exceptions.ConnectionError:
                logger.warning(f"Attempt {attempt + 1}/{max_retries}: Connection error")
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))
                    continue
                self._config_fetch_error = "connection"
                return None

            except RecursionError:
                logger.warning(f"Attempt {attempt + 1}/{max_retries}: Recursion error")
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                self._config_fetch_error = "recursion"
                return None

            except Exception as e:
                error_type = type(e).__name__
                logger.warning(f"Attempt {attempt + 1}/{max_retries}: {error_type}")
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                self._config_fetch_error = f"unknown: {error_type}"
                return None

        # Should never reach here, but safety fallback
        logger.warning(f"All {max_retries} attempts failed")
        return None

    def extract_model_size_from_name(self, model_name: str) -> Optional[float]:
        """
        Extract model size in billions of parameters from model name.

        Args:
            model_name: HuggingFace model name (e.g., "meta-llama/Meta-Llama-3.1-70B-Instruct")

        Returns:
            Model size in billions of parameters, or None if not found
        """
        import re

        model_lower = model_name.lower()

        # Mixtral MoE: effective VRAM = (num_experts × expert_size) / 1.2
        if 'mixtral' in model_lower:
            match = re.search(r'(\d+)x(\d+)b', model_lower)
            if match:
                num_experts = int(match.group(1))
                expert_size = int(match.group(2))
                effective_size = (num_experts * expert_size) / 1.2
                logger.info(f"Mixtral MoE model detected: {num_experts}x{expert_size}B experts → ~{effective_size:.0f}B effective")
                return effective_size

        # Standard pattern: "70B", "8B", etc.
        match = re.search(r'[-_]?(\d+\.?\d*)b[-_]?', model_name, re.IGNORECASE)
        if match:
            size = float(match.group(1))
            logger.info(f"Extracted model size from name: {size}B parameters")
            return size

        # Qwen3 special case: "235B-A22B" → 235B
        match = re.search(r'(\d+)b-a\d+b', model_name, re.IGNORECASE)
        if match:
            size = float(match.group(1))
            logger.info(f"Extracted model size from name: {size}B parameters")
            return size

        logger.warning(f"Could not extract model size from name: {model_name}")
        return None

    def detect_dtype_from_name(self, model_name: str) -> str:
        """
        Detect data type from model name.

        Args:
            model_name: HuggingFace model name

        Returns:
            Data type string ('fp8', 'fp16', 'int8', 'int4')
        """
        model_lower = model_name.lower()

        if 'fp8' in model_lower or 'f8' in model_lower:
            return 'fp8'
        elif 'int8' in model_lower or 'i8' in model_lower:
            return 'int8'
        elif 'int4' in model_lower or 'i4' in model_lower or 'awq' in model_lower or 'gptq' in model_lower:
            return 'int4'
        elif 'fp16' in model_lower or 'f16' in model_lower:
            return 'fp16'
        else:
            # Default to fp16 for most models
            return 'fp16'

    def calculate_kv_cache_from_config(
        self,
        config: Dict,
        isl: int,
        osl: int,
        byte_size: int
    ) -> Optional[float]:
        """
        Calculate KV cache using actual model architecture from config.

        Args:
            config: Model config dict from HuggingFace
            isl: Input sequence length
            osl: Output sequence length
            byte_size: Bytes per parameter (2 for FP16, 1 for FP8, etc.)

        Returns:
            KV cache in GB, or None if config is incomplete
        """
        try:
            # Extract architecture parameters
            num_layers = config.get('num_hidden_layers')
            num_kv_heads = config.get('num_key_value_heads')

            # Fallback: if num_key_value_heads not present, assume no GQA
            if num_kv_heads is None:
                num_kv_heads = config.get('num_attention_heads')

            # Get head dimension
            hidden_size = config.get('hidden_size')
            num_attention_heads = config.get('num_attention_heads')

            if not all([num_layers, num_kv_heads, hidden_size, num_attention_heads]):
                logger.warning(f"Incomplete config: layers={num_layers}, kv_heads={num_kv_heads}, hidden={hidden_size}, attn_heads={num_attention_heads}")
                return None

            head_dim = hidden_size // num_attention_heads
            total_sequence_length = isl + osl

            # Real formula: 2 (K+V) × layers × kv_heads × head_dim × seq_len × bytes
            kv_cache_elements = 2 * num_layers * num_kv_heads * head_dim * total_sequence_length
            kv_cache_bytes = kv_cache_elements * byte_size
            kv_cache_gb = kv_cache_bytes / (1024**3)

            logger.info("KV cache calculation from config:")
            logger.info(f"  Layers: {num_layers}, KV heads: {num_kv_heads}, Head dim: {head_dim}")
            logger.info(f"  Sequence length: {total_sequence_length}")
            logger.info(f"  KV cache: {kv_cache_gb:.2f} GB")

            return kv_cache_gb

        except Exception as e:
            logger.warning(f"Failed to calculate KV cache from config: {e}")
            return None

    def calculate_vram_requirements(
        self,
        model_size_b: float,
        dtype: str = 'fp16',
        isl: int = 2048,
        osl: int = 512,
        model_config: Optional[Dict] = None
    ) -> Tuple[float, float, float, float]:
        """
        Calculate VRAM requirements for model with dynamic KV cache based on sequence length.

        Args:
            model_size_b: Model size in billions of parameters
            dtype: Data type (fp16, fp8, int8, int4)
            isl: Input sequence length (prompt tokens)
            osl: Output sequence length (generation tokens)
            model_config: Optional model config from HuggingFace (for accurate KV cache)

        Returns:
            Tuple of (total_vram_gb, kv_cache_gb, activations_gb, cuda_overhead_gb)
        """
        bytes_per_param = {
            'fp16': 2,
            'fp8': 1,
            'int8': 1,
            'int4': 0.5,
            'fp32': 4,
        }

        byte_size = bytes_per_param.get(dtype, 2)
        model_weights_gb = (model_size_b * 1e9 * byte_size) / (1024**3)
        total_sequence_length = isl + osl

        # Try to calculate KV cache from config first
        kv_cache_gb = None
        if model_config:
            kv_cache_gb = self.calculate_kv_cache_from_config(
                model_config, isl, osl, byte_size
            )

        # Fallback: Conservative estimate for modern LLMs with GQA
        # Most models (Llama 3, Qwen, GLM) use ~8 KV heads regardless of size
        # Conservative assumption: 40 layers, 8 KV heads, 128 head_dim (common for 7B-70B models)
        if kv_cache_gb is None:
            logger.warning("Using conservative KV cache estimate (40 layers, 8 KV heads, 128 head_dim)")
            estimated_layers = 40
            estimated_kv_heads = 8
            estimated_head_dim = 128
            kv_cache_elements = 2 * estimated_layers * estimated_kv_heads * estimated_head_dim * total_sequence_length
            kv_cache_bytes = kv_cache_elements * byte_size
            kv_cache_gb = kv_cache_bytes / (1024**3)

        # Activations (relatively small, ~15% of weights for inference)
        activations_gb = model_weights_gb * 0.15

        # CUDA kernels and framework overhead (~5%)
        cuda_overhead_gb = model_weights_gb * 0.05

        total_vram_gb = model_weights_gb + kv_cache_gb + activations_gb + cuda_overhead_gb

        kv_cache_percent = (kv_cache_gb / model_weights_gb) * 100 if model_weights_gb > 0 else 0

        logger.info("Model VRAM calculation:")
        logger.info(f"  Size: {model_size_b}B parameters")
        logger.info(f"  dtype: {dtype} ({byte_size} bytes/param)")
        logger.info(f"  Sequence: ISL={isl}, OSL={osl}, Total={total_sequence_length}")
        logger.info(f"  Weights: {model_weights_gb:.1f} GB")
        logger.info(f"  KV cache ({kv_cache_percent:.1f}%): {kv_cache_gb:.1f} GB")
        logger.info(f"  Activations (15%): {activations_gb:.1f} GB")
        logger.info(f"  CUDA overhead (5%): {cuda_overhead_gb:.1f} GB")
        logger.info(f"  Total VRAM: {total_vram_gb:.1f} GB (20% overhead)")

        return total_vram_gb, kv_cache_gb, activations_gb, cuda_overhead_gb, model_weights_gb

    def calculate_model_requirements(
        self,
        model_name: str,
        gpu_vram_gb: float = 80,
        isl: int = 2048,
        osl: int = 512,
        num_users: int = 100,
        hf_token: Optional[str] = None
    ) -> ModelRequirements:
        """
        Calculate model requirements including min GPUs and TP.

        Args:
            model_name: HuggingFace model name
            gpu_vram_gb: VRAM per GPU in GB (from cluster scan)
            isl: Input sequence length (prompt tokens)
            osl: Output sequence length (generation tokens)
            num_users: Concurrent users for KV cache calculation
            hf_token: Optional HuggingFace token for private/gated models

        Returns:
            ModelRequirements object
        """
        # Extract model size from name
        model_size_b = self.extract_model_size_from_name(model_name)

        if model_size_b is None:
            # Fallback: assume medium-large model if we can't extract size
            logger.warning(f"Could not determine model size for {model_name}, assuming 70B")
            model_size_b = 70.0

        # Detect dtype from model name
        dtype = self.detect_dtype_from_name(model_name)

        # Fetch model config for accurate KV cache calculation
        # Check if config was read from PVC first (for offline/air-gapped mode)
        model_config = getattr(self, '_pvc_model_config', None)
        if model_config:
            logger.info("Using model config from PVC")
        else:
            # Try to fetch from HuggingFace (with token if provided)
            model_config = self.fetch_model_config(model_name, hf_token)

        # Store config for later access (e.g., for detailed logging)
        self._last_model_config = model_config

        total_vram_gb, kv_cache_gb, activations_gb, cuda_overhead_gb, model_weights_gb = self.calculate_vram_requirements(
            model_size_b, dtype, isl, osl, model_config
        )

        # Calculate dynamic gpu_memory_utilization using capacity-based formula:
        # Setting = (Weights + (Cache_Per_Request × Max_Parallel_Users) + Buffer) / Total_VRAM
        #
        # Where:
        # - Base = Model Weights + CUDA Overhead + Activations (fixed)
        # - Cache_Per_Request = KV cache for single request (ISL + OSL)
        # - Max_Parallel_Users = Estimated concurrent requests
        # - Buffer = GPU_VRAM × 0.05 (5% safety buffer for fragmentation)

        base_requirements = model_weights_gb + cuda_overhead_gb + activations_gb
        safety_buffer_gb = gpu_vram_gb * 0.05
        cache_per_request_gb = kv_cache_gb  # KV cache for single request

        # Use actual concurrent users from configuration
        max_parallel_users = num_users

        # Calculate total cache needed for parallel users
        total_cache_gb = max_parallel_users * cache_per_request_gb

        # Final calculation
        total_target_gb = base_requirements + total_cache_gb + safety_buffer_gb
        gpu_memory_utilization = min(0.95, total_target_gb / gpu_vram_gb)  # Cap at 0.95

        logger.info("GPU Memory Utilization calculation:")
        logger.info(f"  Base (weights + overhead + activations): {base_requirements:.1f} GB")
        logger.info(f"  Cache per request (ISL={isl} + OSL={osl}): {cache_per_request_gb:.2f} GB")
        logger.info(f"  Concurrent users (from config): {max_parallel_users}")
        logger.info(f"  Total cache needed ({max_parallel_users} users): {total_cache_gb:.1f} GB")
        logger.info(f"  Safety buffer (5% of {gpu_vram_gb:.0f}GB GPU): {safety_buffer_gb:.1f} GB")
        logger.info(f"  Total target: {total_target_gb:.1f} GB")
        logger.info(f"  GPU memory utilization: {gpu_memory_utilization:.2f} ({gpu_memory_utilization*100:.0f}%)")

        # Calculate minimum GPUs needed (round up)
        min_gpus = max(1, int((total_vram_gb + gpu_vram_gb - 0.01) / gpu_vram_gb))

        # TP must be power of 2
        min_tp = next_power_of_2(min_gpus)

        # Recommended TP options (powers of 2 up to reasonable max)
        recommended_tp_options = []
        tp = min_tp
        while tp <= 8:  # Don't recommend TP > 8
            recommended_tp_options.append(tp)
            tp *= 2

        # VRAM per GPU when using min_tp
        vram_per_gpu = total_vram_gb / min_tp

        logger.info("Model requirements:")
        logger.info(f"  Total VRAM needed: {total_vram_gb:.1f} GB")
        logger.info(f"  GPU VRAM available: {gpu_vram_gb:.1f} GB per GPU")
        logger.info(f"  Minimum GPUs: {min_gpus}")
        logger.info(f"  Minimum TP: {min_tp}")
        logger.info(f"  VRAM per GPU (TP={min_tp}): {vram_per_gpu:.1f} GB")

        return ModelRequirements(
            model_name=model_name,
            estimated_vram_gb=vram_per_gpu,
            min_gpus=min_gpus,
            min_tp=min_tp,
            recommended_tp_options=recommended_tp_options,
            model_size_b=model_size_b,
            dtype=dtype,
            total_vram_gb=total_vram_gb,
            gpu_vram_gb=gpu_vram_gb,
            kv_cache_gb=kv_cache_gb,
            activations_gb=activations_gb,
            cuda_overhead_gb=cuda_overhead_gb,
            isl=isl,
            osl=osl,
            gpu_memory_utilization=gpu_memory_utilization
        )

    def validate_resources(self, model_requirements: ModelRequirements, max_gpus_to_use: int) -> Tuple[bool, Optional[str]]:
        """
        Validate that user has enough GPUs to run the model.

        Args:
            model_requirements: Model requirements
            max_gpus_to_use: Maximum GPUs user wants to use

        Returns:
            Tuple of (can_proceed, error_message)
        """
        min_needed = model_requirements.min_gpus

        if max_gpus_to_use < min_needed:
            error_msg = (
                f"❌ Insufficient GPU resources!\n\n"
                f"Model: {model_requirements.model_name}\n"
                f"Estimated VRAM per GPU (TP={model_requirements.min_tp}): {model_requirements.estimated_vram_gb:.1f} GB\n"
                f"Minimum GPUs needed: {min_needed}\n"
                f"Maximum GPUs selected: {max_gpus_to_use}\n\n"
                f"💡 Please increase 'Maximum GPUs to Use' to at least {min_needed} GPUs"
            )
            return False, error_msg

        return True, None

    def plan_tests(
        self,
        model_name: str,
        optimization_goal: str,
        max_gpus_to_use: int,
        gpu_vram_gb: float = 80,
        isl: int = 2048,
        osl: int = 512,
        num_users: int = 100,
        hf_token: Optional[str] = None,
        max_gpus_per_node: int = 8,
        cloud_provider: CloudProvider = CloudProvider.UNKNOWN,
        node_count: int = 1,
        kubectl_runner=None
    ) -> TestPlan:
        """
        Plan all tests for recipe-based optimization run.

        Implements the 7-step P/D disaggregation recipe:
        - Step 2: Decode Pareto sweep (aggregated, ISL=1, OSL=target)
        - Step 3: Prefill efficiency sweep (aggregated, ISL=target, OSL=1)
        - Steps 4-5: Analytical resource sizing (mathematical, no tests)
        - Step 7: P/D validation (generated after steps 2-3 complete)

        Args:
            model_name: HuggingFace model name
            optimization_goal: 'throughput', 'ttft', 'balanced'
            max_gpus_to_use: Maximum GPUs user wants to use
            gpu_vram_gb: VRAM per GPU in GB
            isl: Input sequence length (prompt tokens)
            osl: Output sequence length (generation tokens)
            num_users: Concurrent users for KV cache calculation
            hf_token: Optional HuggingFace token for private/gated models
            kubectl_runner: Optional KubectlRunner for provider/network detection

        Returns:
            TestPlan with recipe-based test configurations
        """
        # Calculate model requirements with dynamic KV cache
        model_req = self.calculate_model_requirements(model_name, gpu_vram_gb, isl, osl, num_users, hf_token)

        # Validate resources
        can_proceed, error_msg = self.validate_resources(model_req, max_gpus_to_use)

        if not can_proceed:
            return TestPlan(
                model_name=model_name,
                total_gpus_available=max_gpus_to_use,
                max_gpus_to_use=max_gpus_to_use,
                optimization_goal=optimization_goal,
                model_requirements=model_req,
                tests=[],
                can_proceed=False,
                error_message=error_msg,
                recipe_mode=True
            )

        # Detect DRANET availability (bypasses IBM Cloud 1 pod per node constraint)
        dranet_available = False
        if kubectl_runner and cloud_provider == CloudProvider.IBM_CLOUD:
            try:
                provider = ProviderRegistry.detect_provider(kubectl_runner=kubectl_runner)
                integrator = NetworkIntegrator(provider, kubectl_runner)
                network_type = integrator._select_network_type()
                dranet_available = (network_type == NetworkType.DRA)
                if dranet_available:
                    logger.info("✅ DRANET detected - IBM Cloud pod-per-node constraint bypassed")
                else:
                    logger.info("⚠️  NAD network - IBM Cloud pod-per-node constraint applies")
            except Exception as e:
                logger.debug(f"Could not detect DRANET: {e}")
                dranet_available = False

        # Generate TP options to explore (powers of 2, respecting min_tp)
        # For aggregated architecture, cap at max_gpus_per_node since TP requires all GPUs in one pod
        max_tp = min(max_gpus_to_use, max_gpus_per_node)
        tp_options = []
        tp = max(1, model_req.min_tp)
        while tp <= max_tp:
            tp_options.append(tp)
            tp *= 2

        logger.info(f"Recipe mode: Will explore TP values {tp_options} (capped at max_gpus_per_node={max_gpus_per_node})")

        # Initialize cloud filtering tracking
        cloud_filtered_count = 0
        cloud_filter_reason = None

        # ==========================================
        # Route to correct architecture based on optimization goal
        # ==========================================
        if optimization_goal == 'ttft':
            logger.info("TTFT optimization → Using PD (Prefill/Decode) architecture as primary")
            _primary_architecture = Architecture.PD
            _baseline_architecture = Architecture.AGGREGATED
        elif optimization_goal == 'throughput':
            logger.info("Throughput optimization → Using EP (Expert Parallelism) architecture as primary")
            _primary_architecture = Architecture.EP
            _baseline_architecture = Architecture.AGGREGATED
        else:  # balanced
            logger.info("Balanced optimization → Testing multiple architectures")
            _primary_architecture = Architecture.AGGREGATED
            _baseline_architecture = None

        # ==========================================
        # Step 2: Decode Pareto Sweep
        # ==========================================
        step2_tests = []

        # For TTFT/PD: Skip aggregated decode tests, go straight to PD
        if optimization_goal == 'ttft':
            # PD decode-focused tests will be in Step 7
            logger.info("Step 2: Skipping aggregated decode tests (using PD instead)")
        else:
            # For throughput/balanced: Use aggregated or EP
            test_arch = Architecture.EP if optimization_goal == 'throughput' else Architecture.AGGREGATED
            for tp in tp_options:
                replicas = max(1, max_gpus_to_use // tp)
                total_gpus = tp * replicas

                test = TestConfiguration(
                    test_name=f"Step 2: Decode Pareto - TP={tp}",
                    architecture=test_arch,
                gpus_required=total_gpus,
                tp=tp,
                prefill_pods=0,
                decode_pods=0,
                ep_pods=replicas,  # In aggregated mode, ep_pods = replicas
                description=f"Decode-focused: aggregated, ISL=1, OSL={osl}, TP={tp}×{replicas} = {total_gpus} GPUs",
                recipe_step=2,
                recipe_phase='decode_pareto',
                workload_isl=1,  # Minimal prefill
                workload_osl=osl,  # Full decode
                adaptive_guided=True
            )
            step2_tests.append(test)

        logger.info(f"Step 2 (Decode Pareto): Generated {len(step2_tests)} candidate tests")
        logger.info("  Optimizer will intelligently select tests from this search space")

        # ==========================================
        # Step 3: Prefill Efficiency Sweep
        # ==========================================
        step3_tests = []

        # For TTFT/PD: Skip aggregated prefill tests, go straight to PD
        if optimization_goal == 'ttft':
            # PD prefill-focused tests will be in Step 7
            logger.info("Step 3: Skipping aggregated prefill tests (using PD instead)")
        else:
            # For throughput/balanced: Use aggregated or EP
            test_arch = Architecture.EP if optimization_goal == 'throughput' else Architecture.AGGREGATED
            for tp in tp_options:
                replicas = max(1, max_gpus_to_use // tp)
                total_gpus = tp * replicas

                arch_name = 'EP' if test_arch == Architecture.EP else 'aggregated'
                test = TestConfiguration(
                    test_name=f"Step 3: Prefill Efficiency - TP={tp}",
                    architecture=test_arch,
                    gpus_required=total_gpus,
                    tp=tp,
                    prefill_pods=0,
                    decode_pods=0,
                    ep_pods=replicas,
                    description=f"Prefill-focused: {arch_name}, ISL={isl}, OSL=1, TP={tp}×{replicas} = {total_gpus} GPUs",
                    recipe_step=3,
                    recipe_phase='prefill_efficiency',
                    workload_isl=isl,  # Full prefill
                    workload_osl=1,  # Minimal decode
                    adaptive_guided=True
                )
                step3_tests.append(test)

        logger.info(f"Step 3 (Prefill Efficiency): Generated {len(step3_tests)} candidate tests")
        if step3_tests:
            logger.info("  Optimizer will intelligently select tests from this search space")

        # ==========================================
        # Steps 4-5: Analytical Resource Sizing (Placeholder)
        # ==========================================
        # These are analytical steps, not tests
        # After Step 3 completes, we'll calculate:
        # - Token work rates (W_p, W_d)
        # - GPU requirements with headroom
        # - Feasible P/D splits
        resource_sizing_placeholder = TestConfiguration(
            test_name="Steps 4-5: Analytical Resource Sizing",
            architecture=Architecture.AGGREGATED,  # Dummy
            gpus_required=0,
            tp=0,
            prefill_pods=0,
            decode_pods=0,
            ep_pods=0,
            description="Mathematical resource sizing - no tests needed",
            recipe_step=4,
            recipe_phase='analytical_resource_sizing',
            adaptive_guided=False
        )

        # ==========================================
        # Step 7: P/D Validation or Primary Tests
        # ==========================================
        step7_tests = []

        if optimization_goal == 'ttft':
            # For TTFT: Generate PD tests as primary optimization target
            logger.info("Step 7: Generating PD tests for TTFT optimization")

            # Track unique configurations to avoid duplicates
            # Key: (prefill_pods, prefill_tp, decode_pods, decode_tp)
            seen_configs = set()

            # Track cloud-filtered tests
            cloud_filtered_tests = []

            # Explore different P/D configurations
            # Strategy: For each (prefill_tp, decode_tp) pair, explore different GPU splits
            for prefill_tp in tp_options:
                for decode_tp in tp_options:
                    # For TTFT optimization, explore different prefill/decode GPU allocations
                    # Test different ratios to find optimal balance for low latency

                    # Calculate max pods we could use for each side
                    _max_prefill_pods = max_gpus_to_use // prefill_tp
                    _max_decode_pods = max_gpus_to_use // decode_tp

                    # Explore different splits: 25%, 33%, 50%, 67%, 75% to prefill
                    # (For TTFT, we want to find the sweet spot - more prefill = lower TTFT but less decode capacity)
                    split_ratios = [0.25, 0.33, 0.5, 0.67, 0.75]

                    for ratio in split_ratios:
                        prefill_gpus = int(max_gpus_to_use * ratio)
                        # Ensure prefill_gpus is a multiple of prefill_tp
                        prefill_pods = max(1, prefill_gpus // prefill_tp)
                        prefill_gpus_actual = prefill_pods * prefill_tp

                        decode_gpus = max_gpus_to_use - prefill_gpus_actual
                        decode_pods = max(1, decode_gpus // decode_tp)
                        decode_gpus_actual = decode_pods * decode_tp

                        total_gpus = prefill_gpus_actual + decode_gpus_actual

                        # Create unique key for this configuration
                        config_key = (prefill_pods, prefill_tp, decode_pods, decode_tp)

                        # Only create test if valid and not a duplicate
                        if (total_gpus <= max_gpus_to_use and
                            prefill_pods >= 1 and decode_pods >= 1 and
                            prefill_gpus_actual >= prefill_tp and decode_gpus_actual >= decode_tp and
                            config_key not in seen_configs):

                            # Mark this config as seen
                            seen_configs.add(config_key)

                            # Validate cloud provider constraints (skip if DRANET bypasses them)
                            if dranet_available:
                                # DRANET bypasses IBM Cloud pod-per-node constraint
                                is_valid = True
                                reason = ""
                            else:
                                # Apply cloud constraints (NAD network)
                                is_valid, reason = validate_pd_config(
                                    cloud_provider=cloud_provider,
                                    prefill_pods=prefill_pods,
                                    decode_pods=decode_pods,
                                    num_nodes=node_count,
                                    prefill_tp=prefill_tp,
                                    decode_tp=decode_tp
                                )

                            # Calculate actual percentage after rounding
                            actual_prefill_pct = int((prefill_gpus_actual / total_gpus) * 100)

                            test = TestConfiguration(
                                test_name=f"Step 7: PD - P{prefill_pods}×TP{prefill_tp} D{decode_pods}×TP{decode_tp}",
                                architecture=Architecture.PD,
                                gpus_required=total_gpus,
                                tp=prefill_tp,  # Store prefill TP in tp field
                                decode_tp=decode_tp,  # Store decode TP separately
                                prefill_pods=prefill_pods,
                                decode_pods=decode_pods,
                                ep_pods=0,
                                description=f"PD: {prefill_pods}×TP{prefill_tp} prefill + {decode_pods}×TP{decode_tp} decode = {total_gpus} GPUs ({actual_prefill_pct}% prefill)",
                                recipe_step=7,
                                recipe_phase='pd_optimization',
                                workload_isl=isl,
                                workload_osl=osl,
                                adaptive_guided=True
                            )

                            if is_valid:
                                step7_tests.append(test)
                            else:
                                # Track filtered test for warning message
                                cloud_filtered_tests.append((test, reason))

            logger.info(f"Step 7 (PD Optimization): Generated {len(step7_tests)} unique PD configurations")
            logger.info("  Exploring different prefill/decode splits to minimize TTFT")

            # Print cloud constraint warning if any tests were filtered
            if cloud_filtered_tests:
                total_generated = len(step7_tests) + len(cloud_filtered_tests)
                constraints = CloudConstraints.get_constraints(cloud_provider)

                # Capture filtering info for TestPlan
                cloud_filtered_count = len(cloud_filtered_tests)

                # Update reason to explain NAD vs DRANET
                if cloud_provider == CloudProvider.IBM_CLOUD and not dranet_available:
                    cloud_filter_reason = f"{constraints.description} (NAD network - use DRANET to bypass)"
                else:
                    cloud_filter_reason = constraints.description

                # Print red warning (both to logger and direct print for visibility)
                network_note = ""
                if cloud_provider == CloudProvider.IBM_CLOUD and not dranet_available:
                    network_note = "\n  💡 Using NAD network - DRANET would bypass this constraint\n"

                warning_msg = f"""
\033[91m{'=' * 80}
⚠️  CLOUD PROVIDER CONSTRAINT: {cloud_provider.value.upper()}
{'=' * 80}
{total_generated} tests were generated, but {len(cloud_filtered_tests)} tests
were FILTERED OUT due to cloud provider constraints:

  {constraints.description}{network_note}
Tests after filtering: {len(step7_tests)}
{'=' * 80}\033[0m
"""
                print(warning_msg)
                logger.warning(warning_msg)
        else:
            # For throughput/balanced: Placeholder for future P/D validation
            step7_placeholder = TestConfiguration(
                test_name="Step 7: P/D Validation",
                architecture=Architecture.PD,
                gpus_required=0,
                tp=0,
                prefill_pods=0,
                decode_pods=0,
                ep_pods=0,
                description="End-to-end validation - tests generated after resource sizing",
                recipe_step=7,
                recipe_phase='pd_validation',
                adaptive_guided=True
            )
            step7_tests.append(step7_placeholder)

        # ==========================================
        # Combine all tests for display
        # ==========================================
        all_tests = []

        if optimization_goal == 'ttft':
            # For TTFT: Show PD tests as primary optimization
            all_tests.extend(step7_tests)
            # Add analytical sizing placeholder for context
            all_tests.append(resource_sizing_placeholder)

            # After PD optimization completes, will add baseline Aggregated test
            # (This happens during execution, not at plan generation time)

            estimated_total = f"{len(step7_tests)}-{len(step7_tests) + 3} tests (adaptive PD optimization + baseline)"

            logger.info("Recipe test plan complete (TTFT optimization):")
            logger.info(f"  Step 7 PD candidates: {len(step7_tests)}")
            logger.info(f"  Estimated total tests: {estimated_total}")
        else:
            # For Throughput/Balanced: Traditional recipe flow
            # Show Step 2 tests first (what will actually run first)
            all_tests = step2_tests.copy()
            # Add Step 3 tests
            all_tests.extend(step3_tests)
            # Add placeholders for context
            all_tests.append(resource_sizing_placeholder)
            all_tests.extend(step7_tests)

            # Calculate estimated test count
            # Step 2: 3-5 tests (adaptive convergence)
            # Step 3: 3-5 tests (adaptive convergence)
            # Steps 4-5: 0 tests (analytical sizing)
            # Step 7: 5-8 tests (adaptive sampling)
            estimated_total = "11-18 tests (recipe-guided)"

            logger.info("Recipe test plan complete:")
            logger.info(f"  Step 2 candidates: {len(step2_tests)}")
            logger.info(f"  Step 3 candidates: {len(step3_tests)}")
            logger.info(f"  Step 7 candidates: {len(step7_tests)}")
            logger.info(f"  Estimated total tests: {estimated_total}")

        return TestPlan(
            model_name=model_name,
            total_gpus_available=max_gpus_to_use,
            max_gpus_to_use=max_gpus_to_use,
            optimization_goal=optimization_goal,
            model_requirements=model_req,
            tests=all_tests,
            can_proceed=True,
            error_message=None,
            recipe_mode=True,
            step2_tests=step2_tests,
            step3_tests=step3_tests,
            step7_tests=step7_tests,
            estimated_total_tests=estimated_total,
            cloud_filtered_count=cloud_filtered_count,
            cloud_filter_reason=cloud_filter_reason,
            cloud_provider=cloud_provider.value if cloud_provider else None
        )
