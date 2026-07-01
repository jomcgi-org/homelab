# model-bench leaderboard

## Agentic leaderboard: qualified

Cleared the easy+standard floor. Ranked by hard-task pass, then cost.

| Model                             | hard | mean tokens | mean turns | wall-time (s) | cost ($) | $/solve | tool-use ok |
| --------------------------------- | ---- | ----------- | ---------- | ------------- | -------- | ------- | ----------- |
| qwen/qwen3-coder-30b-a3b-instruct | 3/3  | 23543       | 8.0        | 32.0          | 0.0022   | 0.0022  | 1.00        |
| google/gemma-4-26b-a4b-it         | 3/3  | 35419       | 7.6        | 69.2          | 0.0029   | 0.0029  | 1.00        |
| deepseek/deepseek-v4-flash        | 3/3  | 29029       | 5.9        | 46.9          | 0.0030   | 0.0030  | 1.00        |
| qwen/qwen3-coder-next             | 3/3  | 43368       | 10.1       | 19.8          | 0.0066   | 0.0066  | 1.00        |
| qwen/qwen3.6-35b-a3b              | 3/3  | 38494       | 7.1        | 42.0          | 0.0092   | 0.0092  | 1.00        |
| z-ai/glm-4.7                      | 3/3  | 25173       | 8.2        | 84.6          | 0.0145   | 0.0145  | 1.00        |
| deepseek/deepseek-v4-pro          | 3/3  | 29043       | 5.9        | 60.7          | 0.0145   | 0.0145  | 1.00        |
| qwen/qwen3.6-27b                  | 3/3  | 53831       | 8.6        | 74.8          | 0.0250   | 0.0250  | 1.00        |
| z-ai/glm-5.2                      | 3/3  | 23471       | 5.4        | 101.7         | 0.0278   | 0.0278  | 0.78        |
| anthropic/claude-sonnet-4.6       | 3/3  | 32329       | 6.2        | 62.7          | 0.1399   | 0.1399  | 1.00        |
| anthropic/claude-opus-4.8         | 3/3  | 21252       | 3.8        | 39.9          | 0.1807   | 0.1807  | 1.00        |

## Agentic leaderboard: disqualified

Failed one or more floor (easy/standard) tasks, so not yet viable.

| Model                   | floor | failed floor tasks                                                                      | tool-use ok |
| ----------------------- | ----- | --------------------------------------------------------------------------------------- | ----------- |
| google/gemma-4-31b-it   | 5/6   | go-vsock-frame-01                                                                       | 1.00        |
| mistralai/devstral-2512 | 4/6   | hikes-walkhighlands-dom-01, worldcup-fixtures-guard-01                                  | 0.89        |
| google/gemini-3.5-flash | 3/6   | hikes-walkhighlands-dom-01, hikes-walkhighlands-duration-01, worldcup-fixtures-guard-01 | 1.00        |

## Budget tier

No qualifying budget candidates yet.

## All results

No results yet.

## Anchors

No anchors defined.

## Pareto frontier

No frontier data yet.

## Retired

| Model                            | final pass@1 | cost ($) | reason                                                                               | date       |
| -------------------------------- | ------------ | -------- | ------------------------------------------------------------------------------------ | ---------- |
| qwen/qwen-2.5-coder-32b-instruct | 0.00         | 0.0000   | 0/15 agentic: OpenRouter provider 4xxes on tool-calling requests, cannot participate | 2026-07-01 |
