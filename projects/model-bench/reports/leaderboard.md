# model-bench leaderboard

## Agentic leaderboard: qualified

Cleared the easy+standard viability floor (at most one miss). Ranked by hard-task pass, then cost.

| Model | hard | mean tokens | mean turns | wall-time (s) | cost ($) | $/solve | tool-use ok |
| --- | --- | --- | --- | --- | --- | --- | --- |
| qwen/qwen3-coder-30b-a3b-instruct | 7/7 | 71151 | 9.1 | 103.2 | 0.0063 | 0.0069 | 0.73 |
| qwen/qwen3-coder-next | 7/7 | 217279 | 17.3 | 44.9 | 0.0279 | 0.0279 | 1.00 |
| deepseek/deepseek-v4-pro | 7/7 | 98685 | 9.7 | 146.4 | 0.0454 | 0.0454 | 1.00 |
| qwen/qwen3.7-plus | 7/7 | 290054 | 13.5 | 331.8 | 0.0992 | 0.0992 | 1.00 |
| qwen/qwen3.8-27b | 6/7 | 113975 | 10.2 | 228.8 | 0.0000 | 0.0000 | 1.00 |
| google/gemma-4-31b-it | 5/7 | 33064 | 6.5 | 260.1 | 0.0050 | 0.0061 | 1.00 |
| deepseek/deepseek-v4-flash | 5/7 | 141741 | 12.2 | 237.6 | 0.0134 | 0.0164 | 1.00 |
| z-ai/glm-4.7 | 5/7 | 95439 | 11.5 | 168.7 | 0.0442 | 0.0540 | 0.91 |
| qwen/qwen3.6-27b | 5/7 | 218089 | 20.5 | 159.2 | 0.0748 | 0.0914 | 1.00 |
| z-ai/glm-5.2 | 5/7 | 103929 | 10.9 | 124.4 | 0.1039 | 0.1270 | 0.91 |
| mistralai/devstral-2512 | 5/7 | 233241 | 17.9 | 94.0 | 0.1064 | 0.1300 | 0.82 |
| tencent/hy3:free | 4/7 | 81872 | 8.7 | 76.6 | 0.0000 | 0.0000 | 0.91 |
| qwen/qwen3.6-35b-a3b | 4/7 | 204711 | 20.9 | 43.4 | 0.0332 | 0.0456 | 1.00 |
| google/gemma-4-26b-a4b-it | 3/7 | 71642 | 9.5 | 199.9 | 0.0055 | 0.0101 | 0.82 |
| google/gemini-3.5-flash | 3/6 | 83563 | 11.9 | 55.9 | 0.2104 | 0.3788 | 1.00 |
| cohere/north-mini-code:free | 1/7 | 80576 | 14.1 | 113.8 | 0.0000 | 0.0000 | 0.82 |

## Agentic leaderboard: disqualified

Missed more than one floor (easy/standard) viability task, so not yet viable.

No disqualified models.

## Frontier ceiling (agentic)

Claude anchors run via Claude Code (Joe pays $0 via the Max subscription), graded by the same verifier. The capability + cost/wall-time ceiling to match and beat, not a ranked competitor. Cost is the representative API rental price; turns/tokens are omitted since the harness differs from the candidate rows.

| Model | hard | pass rate | wall-time (s) | rental ($) | tasks |
| --- | --- | --- | --- | --- | --- |
| anthropic/claude-sonnet-4.6 | 7/7 | 1.00 | 57.6 | 0.7625 | 11 |
| anthropic/claude-opus-4.8 | 7/7 | 1.00 | 58.1 | 0.7779 | 11 |

## Budget tier

No qualifying budget candidates yet.

## All results

| Model | Class | pass@1 | cost ($) | tier | qualifies |
| --- | --- | --- | --- | --- | --- |
| cohere/north-mini-code:free | config-plumbing | 0.00 | 0.0000 | can't | no |
| qwen/qwen3.8-27b | config-plumbing | 0.50 | 0.0000 | needs-repair | no |
| tencent/hy3:free | config-plumbing | 0.50 | 0.0000 | can't | no |
| qwen/qwen3-coder-30b-a3b-instruct | config-plumbing | 0.50 | 0.0020 | can't | no |
| deepseek/deepseek-v4-flash | config-plumbing | 0.50 | 0.0027 | can't | no |
| google/gemma-4-26b-a4b-it | config-plumbing | 0.50 | 0.0035 | can't | no |
| google/gemma-4-31b-it | config-plumbing | 0.50 | 0.0047 | can't | no |
| qwen/qwen3-coder-next | config-plumbing | 0.50 | 0.0080 | can't | no |
| qwen/qwen3.6-35b-a3b | config-plumbing | 0.50 | 0.0103 | can't | no |
| deepseek/deepseek-v4-pro | config-plumbing | 0.50 | 0.0129 | can't | no |
| mistralai/devstral-2512 | config-plumbing | 0.50 | 0.0218 | can't | no |
| qwen/qwen3.7-plus | config-plumbing | 0.50 | 0.0225 | can't | no |
| z-ai/glm-4.7 | config-plumbing | 0.50 | 0.0225 | can't | no |
| qwen/qwen3.6-27b | config-plumbing | 0.00 | 0.0295 | can't | no |
| z-ai/glm-5.2 | config-plumbing | 0.50 | 0.0374 | can't | no |
| cohere/north-mini-code:free | free-text | 1.00 | 0.0000 | one-shots | no |
| qwen/qwen3.8-27b | free-text | 1.00 | 0.0000 | one-shots | no |
| tencent/hy3:free | free-text | 1.00 | 0.0000 | one-shots | no |
| qwen/qwen3-coder-30b-a3b-instruct | free-text | 1.00 | 0.0000 | one-shots | no |
| google/gemma-4-26b-a4b-it | free-text | 1.00 | 0.0000 | one-shots | no |
| google/gemma-4-31b-it | free-text | 1.00 | 0.0000 | one-shots | no |
| qwen/qwen3-coder-next | free-text | 1.00 | 0.0001 | one-shots | no |
| deepseek/deepseek-v4-flash | free-text | 1.00 | 0.0001 | one-shots | no |
| mistralai/devstral-2512 | free-text | 1.00 | 0.0002 | one-shots | no |
| deepseek/deepseek-v4-pro | free-text | 1.00 | 0.0007 | one-shots | no |
| z-ai/glm-4.7 | free-text | 1.00 | 0.0019 | one-shots | no |
| z-ai/glm-5.2 | free-text | 1.00 | 0.0019 | one-shots | no |
| qwen/qwen3.7-plus | free-text | 1.00 | 0.0019 | one-shots | no |
| qwen/qwen3.6-35b-a3b | free-text | 1.00 | 0.0020 | one-shots | no |
| qwen/qwen3.6-27b | free-text | 1.00 | 0.0050 | one-shots | no |

## Anchors

| Model | Class | pass@1 | cost ($) |
| --- | --- | --- | --- |
| anthropic/claude-sonnet-4.6 | free-text | 1.00 | 0.0000 |
| anthropic/claude-sonnet-4.6 | config-plumbing | 1.00 | 0.0000 |
| anthropic/claude-opus-4.8 | config-plumbing | 1.00 | 0.0000 |

## Pareto frontier

**config-plumbing:** qwen/qwen3.8-27b, tencent/hy3:free
**free-text:** cohere/north-mini-code:free, qwen/qwen3.8-27b, tencent/hy3:free

## Retired

| Model | final pass@1 | cost ($) | reason | date |
| --- | --- | --- | --- | --- |
| qwen/qwen-2.5-coder-32b-instruct | 0.00 | 0.0000 | 0/15 agentic: OpenRouter provider 4xxes on tool-calling requests, cannot participate | 2026-07-01 |
