"""Multiple-choice knowledge and reasoning: GPQA Diamond and MMLU-Pro."""

import ast

from .base import Item, Published, Task, choice_answer, parquet_rows

LETTERS = "ABCDEFGHIJ"
CHOICE_INSTRUCTION = (
    "Answer the following multiple choice question. Think step by step, then give your final "
    "answer on the last line in the form 'Answer: X', where X is one of {letters}."
)


class GPQADiamond(Task):
    name = "gpqa_diamond"
    description = "GPQA Diamond (198 graduate-level science questions, 4 choices)"
    defaults = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "max_tokens": 65536, "samples": 1, "thinking": True}
    published = [
        Published(
            91.7,
            "accuracy",
            "BF16 base model, thinking; Qwen's harness and sample count not stated",
            "Qwen/Qwen3.8-Flash-Next model card",
        ),
    ]

    def load(self, args):
        # Ungated mirror of Idavidrein/gpqa (diamond) with choices already shuffled into the question.
        rows = parquet_rows("fingertap/GPQA-Diamond", "test/gpqa_diamond.parquet")
        prompt = CHOICE_INSTRUCTION.format(letters="A, B, C or D")
        return [
            Item(id=str(i), messages=[{"role": "user", "content": f"{prompt}\n\n{r['question']}"}], target=r["answer"])
            for i, r in enumerate(rows)
        ]

    def score(self, item, content):
        pred = choice_answer(content, "ABCD")
        return float(pred == item.target), pred


class MMLUPro(Task):
    name = "mmlu_pro"
    description = "MMLU-Pro test set (12,032 questions, up to 10 choices, 14 categories)"
    defaults = {"temperature": 0.0, "max_tokens": 8192, "samples": 1}
    published = []

    @classmethod
    def add_args(cls, parser):
        parser.add_argument(
            "--mmlu-pro-categories",
            default="",
            help="comma-separated categories to keep (e.g. math,law); default all",
        )

    def load(self, args):
        rows = parquet_rows("TIGER-Lab/MMLU-Pro", "data/test-00000-of-00001.parquet")
        keep = {c.strip() for c in args.mmlu_pro_categories.split(",") if c.strip()}
        items = []
        for r in rows:
            if keep and r["category"] not in keep:
                continue
            options = r["options"]
            if isinstance(options, str):
                options = ast.literal_eval(options)
            letters = LETTERS[: len(options)]
            body = "\n".join(f"{letters[i]}. {o}" for i, o in enumerate(options))
            prompt = CHOICE_INSTRUCTION.format(letters=", ".join(letters))
            items.append(
                Item(
                    id=str(r["question_id"]),
                    messages=[{"role": "user", "content": f"{prompt}\n\n{r['question']}\n\n{body}"}],
                    target=r["answer"],
                    meta={"category": r["category"], "letters": letters},
                )
            )
        return items

    def score(self, item, content):
        pred = choice_answer(content, item.meta["letters"])
        return float(pred == item.target), pred
