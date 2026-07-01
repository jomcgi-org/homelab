# model-bench leaderboard

## Agentic leaderboard: qualified

Cleared the easy+standard floor. Ranked by hard-task pass, then cost.

| Model                             | hard | mean tokens | mean turns | wall-time (s) | cost ($) | $/solve | tool-use ok |
| --------------------------------- | ---- | ----------- | ---------- | ------------- | -------- | ------- | ----------- |
| qwen/qwen3-coder-30b-a3b-instruct | 2/2  | 21607       | 6.4        | 34.2          | 0.0021   | 0.0021  | 1.00        |
| google/gemma-4-26b-a4b-it         | 2/2  | 21126       | 6.4        | 75.4          | 0.0022   | 0.0022  | 1.00        |
| deepseek/deepseek-v4-flash        | 2/2  | 27934       | 4.6        | 48.2          | 0.0030   | 0.0030  | 1.00        |
| qwen/qwen3-coder-next             | 2/2  | 34660       | 8.4        | 19.3          | 0.0060   | 0.0060  | 1.00        |
| qwen/qwen3.6-35b-a3b              | 2/2  | 43502       | 6.9        | 50.5          | 0.0107   | 0.0107  | 1.00        |
| z-ai/glm-4.7                      | 2/2  | 24363       | 6.9        | 92.4          | 0.0150   | 0.0150  | 1.00        |
| deepseek/deepseek-v4-pro          | 2/2  | 31044       | 5.1        | 67.5          | 0.0158   | 0.0158  | 1.00        |
| qwen/qwen3.6-27b                  | 2/2  | 62332       | 8.4        | 76.2          | 0.0292   | 0.0292  | 1.00        |
| z-ai/glm-5.2                      | 2/2  | 25367       | 4.7        | 119.1         | 0.0307   | 0.0307  | 0.71        |
| anthropic/claude-sonnet-4.6       | 2/2  | 30454       | 5.0        | 69.8          | 0.1408   | 0.1408  | 1.00        |
| anthropic/claude-opus-4.8         | 2/2  | 21466       | 3.1        | 44.9          | 0.1955   | 0.1955  | 1.00        |
| google/gemma-4-31b-it             | 1/2  | 17915       | 6.0        | 148.9         | 0.0029   | 0.0033  | 1.00        |

## Agentic leaderboard: disqualified

Failed one or more floor (easy/standard) tasks, so not yet viable.

| Model                   | floor | failed floor tasks                                                                      | tool-use ok |
| ----------------------- | ----- | --------------------------------------------------------------------------------------- | ----------- |
| mistralai/devstral-2512 | 3/5   | hikes-walkhighlands-dom-01, worldcup-fixtures-guard-01                                  | 0.86        |
| google/gemini-3.5-flash | 2/5   | hikes-walkhighlands-dom-01, hikes-walkhighlands-duration-01, worldcup-fixtures-guard-01 | 1.00        |

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
