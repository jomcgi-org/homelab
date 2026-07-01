# model-bench leaderboard

## Agentic leaderboard: qualified

Cleared the easy+standard floor. Ranked by hard-task pass, then cost.

| Model | hard | median tokens | median turns | wall-time (s) | cost ($) | $/solve | tool-use ok |
| --- | --- | --- | --- | --- | --- | --- | --- |
| qwen/qwen3-coder-30b-a3b-instruct | 2/2 | 17844 | 5.0 | 33.8 | 0.0021 | 0.0021 | 1.00 |
| google/gemma-4-26b-a4b-it | 2/2 | 19802 | 6.0 | 52.6 | 0.0022 | 0.0022 | 1.00 |
| deepseek/deepseek-v4-flash | 2/2 | 18903 | 4.0 | 40.0 | 0.0030 | 0.0030 | 1.00 |
| qwen/qwen3-coder-next | 2/2 | 15574 | 3.0 | 16.0 | 0.0060 | 0.0060 | 1.00 |
| qwen/qwen3.6-35b-a3b | 2/2 | 45127 | 7.0 | 43.1 | 0.0107 | 0.0107 | 1.00 |
| z-ai/glm-4.7 | 2/2 | 24840 | 6.0 | 96.6 | 0.0150 | 0.0150 | 1.00 |
| deepseek/deepseek-v4-pro | 2/2 | 20335 | 4.0 | 56.0 | 0.0158 | 0.0158 | 1.00 |
| qwen/qwen3.6-27b | 2/2 | 42886 | 7.0 | 66.5 | 0.0292 | 0.0292 | 1.00 |
| z-ai/glm-5.2 | 2/2 | 18815 | 4.0 | 129.3 | 0.0307 | 0.0307 | 0.71 |
| anthropic/claude-sonnet-4.6 | 2/2 | 28849 | 5.0 | 77.3 | 0.1408 | 0.1408 | 1.00 |
| anthropic/claude-opus-4.8 | 2/2 | 23501 | 3.0 | 52.8 | 0.1955 | 0.1955 | 1.00 |
| google/gemma-4-31b-it | 1/2 | 17171 | 5.0 | 83.3 | 0.0029 | 0.0033 | 1.00 |

## Agentic leaderboard: disqualified

Failed one or more floor (easy/standard) tasks, so not yet viable.

| Model | floor | failed floor tasks | tool-use ok |
| --- | --- | --- | --- |
| mistralai/devstral-2512 | 3/5 | hikes-walkhighlands-dom-01, worldcup-fixtures-guard-01 | 0.86 |
| google/gemini-3.5-flash | 2/5 | hikes-walkhighlands-dom-01, hikes-walkhighlands-duration-01, worldcup-fixtures-guard-01 | 1.00 |
| qwen/qwen-2.5-coder-32b-instruct | 0/5 | flights-module-01, hikes-walkhighlands-dom-01, hikes-walkhighlands-duration-01, slo-budget-breach-01, worldcup-fixtures-guard-01 | 0.00 |

## Budget tier

No qualifying budget candidates yet.

## All results

No results yet.

## Anchors

No anchors defined.

## Pareto frontier

No frontier data yet.

## Retired

No retired models.
