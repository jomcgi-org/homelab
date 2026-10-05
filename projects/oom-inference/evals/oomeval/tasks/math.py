"""Math word problems and competition math: GSM8K and AIME."""

from .base import BOXED_INSTRUCTION, Item, Published, Task, numeric_answer, parquet_rows

CARD = "RadixArk Qwen3.8-Flash-Next-NVFP4 model card (this checkpoint, SGLang)"


class GSM8K(Task):
    name = "gsm8k"
    description = "GSM8K test set (1319 grade-school math problems), boxed final answer"
    defaults = {"temperature": 0.6, "top_p": 0.95, "max_tokens": 8192, "samples": 1}
    published = [
        Published(97.27, "accuracy", "full 1319, t0.6 / top-p 0.95 / max 8192, template-default thinking", CARD),
    ]

    def load(self, args):
        rows = parquet_rows("openai/gsm8k", "main/test-00000-of-00001.parquet")
        return [
            Item(
                id=str(i),
                messages=[{"role": "user", "content": f"{r['question']}\n{BOXED_INSTRUCTION}"}],
                target=r["answer"].split("####")[-1].strip().replace(",", ""),
            )
            for i, r in enumerate(rows)
        ]

    def score(self, item, content):
        pred = numeric_answer(content)
        return float(pred is not None and pred == numeric_answer(item.target)), pred


class AIME(Task):
    name = "aime26"
    description = "AIME 2026 (30 problems, MathArena), integer answers 0-999"
    defaults = {"temperature": 1.0, "top_p": 0.95, "max_tokens": 130000, "samples": 8, "thinking": True}
    published = [
        Published(98.75, "pass@1 (mean over samples)", "30 x 8, t1.0 / top-p 0.95 / max 130k, thinking", CARD),
    ]
    dataset = "MathArena/aime_2026"

    def load(self, args):
        rows = parquet_rows(self.dataset, "data/train-00000-of-00001.parquet")
        return [
            Item(
                id=str(r["problem_idx"]),
                messages=[{"role": "user", "content": f"{r['problem']}\n{BOXED_INSTRUCTION}"}],
                target=str(r["answer"]).strip(),
            )
            for r in rows
        ]

    def score(self, item, content):
        pred = numeric_answer(content)
        return float(pred is not None and pred == numeric_answer(item.target)), pred


class AIME25(AIME):
    name = "aime25"
    description = "AIME 2025 (30 problems, MathArena), integer answers 0-999"
    published = []
    dataset = "MathArena/aime_2025"
