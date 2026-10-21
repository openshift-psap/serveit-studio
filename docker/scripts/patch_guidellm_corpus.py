#!/usr/bin/env python3
"""Patch guidellm's synthetic_text generator to draw real prose from the
baked-in wikitext-103 corpus instead of faker lorem-ipsum.

Applied at workload-image build time (docker/Dockerfile.workload). The
token-budgeting logic is unchanged: prompts are still trimmed to the exact
requested token count via the tokenizer — only the source text becomes
real prose. Prefix buckets, turns, delays, and distributions all keep
working because they layer on top of _create_prompt.

Falls back to faker when the corpus file is missing, so the image stays
usable without the corpus.
"""

import re
import sys
from pathlib import Path

CORPUS_PATCH = '''

    # ---- ServeIt corpus patch (real wikitext-103 prose) ----
    _CORPUS_PATH = os.environ.get("GUIDELLM_CORPUS_PATH", "/app/corpus/wikitext-103.txt")
    _corpus_state = None

    @classmethod
    def _load_corpus_pool(cls, processor):
        """Load and pre-tokenize a corpus pool (lazy, once per process).

        Streams a bounded 24MB text window (~5M tokens) instead of reading
        the whole ~500MB corpus file — loading it whole OOMs the workload
        pod. The pool keeps the first ~2M tokens of paragraph text: plenty
        of diversity for benchmark prompts while keeping pre-encoding
        under a few seconds.
        """
        if cls._corpus_state is not None:
            return cls._corpus_state
        try:
            with open(cls._CORPUS_PATH, encoding="utf-8", errors="ignore") as f:
                text = f.read(24 * 1024 * 1024)
            # The corpus file is line-oriented (one wikitext paragraph per
            # line, no blank-line separators). Group short lines into
            # ~1.2k-char chunks so pool entries are diverse and reasonably
            # sized.
            lines = [ln.strip() for ln in text.split("\\n") if len(ln.strip()) >= 80]
            del text
            chunks = []
            cur, cur_len = [], 0
            for ln in lines:
                cur.append(ln)
                cur_len += len(ln)
                if cur_len >= 1200:
                    chunks.append(" ".join(cur))
                    cur, cur_len = [], 0
            if cur:
                chunks.append(" ".join(cur))
            del lines
            if not chunks:
                cls._corpus_state = ([], None)
                return cls._corpus_state
            pool = []
            total = 0
            budget = 2_000_000
            for chunk in chunks:
                ids = processor.encode(chunk)
                pool.append(ids)
                total += len(ids)
                if total >= budget:
                    break
            del chunks
            cls._corpus_state = (pool, Random(20261021))
        except Exception:
            cls._corpus_state = ([], None)
        return cls._corpus_state

    def _corpus_prompt(self, prompt_tokens_count: int, unique: str = ""):
        """Build a prompt from real corpus text, exact token count.

        Returns None when the corpus is unavailable (caller falls back
        to faker).
        """
        pool, rng = self._load_corpus_pool(self.processor)
        if not pool or rng is None:
            return None
        ids: list[int] = []
        if unique:
            ids.extend(self.processor.encode(unique))
        while len(ids) < prompt_tokens_count:
            ids.extend(rng.choice(pool))
        return self.processor.decode(  # type: ignore[return-value]
            ids[:prompt_tokens_count], skip_special_tokens=True
        )

    def _create_prompt(
        self, prompt_tokens_count: int, faker: Faker, unique: str = ""
    ) -> str:
        corpus_text = self._corpus_prompt(prompt_tokens_count, unique)
        if corpus_text is not None:
            return corpus_text

        prompt_token_ids: list[int] = []
        avg_chars_per_token = 5
        margin_of_safety = 1.5
        attempts = 0

        while len(prompt_token_ids) < prompt_tokens_count:
            attempts += 1
            num_chars = int(
                prompt_tokens_count * avg_chars_per_token * margin_of_safety * attempts
            )
            text = unique + faker.text(max_nb_chars=num_chars)
            prompt_token_ids = self.processor.encode(text)

        return self.processor.decode(  # type: ignore[return-value]
            prompt_token_ids[:prompt_tokens_count], skip_special_tokens=True
        )
'''


def main() -> int:
    try:
        import guidellm
    except ImportError:
        print("guidellm not installed — nothing to patch", file=sys.stderr)
        return 1

    target = Path(guidellm.__file__).parent / "data" / "deserializers" / "synthetic.py"
    src = target.read_text()

    if "_corpus_prompt" in src:
        print(f"{target} already patched")
        return 0

    # Replace the original _create_prompt method (from its def line up to
    # the next method at the same indent level) with the corpus version.
    pattern = re.compile(
        r"    def _create_prompt\(\n(?:.*\n)*?    def _create_prefix_iter\(",
        re.M,
    )
    match = pattern.search(src)
    if not match:
        print(
            "could not locate _create_prompt in synthetic.py — "
            "guidellm version drift, aborting patch",
            file=sys.stderr,
        )
        return 1

    replacement = CORPUS_PATCH + "\n    def _create_prefix_iter("
    src = src[: match.start()] + replacement + src[match.end() :]

    # The patch uses os.environ and Random at class level
    if "\nimport os\n" not in src and "import os" not in src.split("class ")[0]:
        src = src.replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations\n\nimport os\n",
            1,
        )

    target.write_text(src)
    print(f"patched {target}: synthetic_text now draws from the wikitext corpus")
    return 0


if __name__ == "__main__":
    sys.exit(main())
