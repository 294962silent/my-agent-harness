# Long-horizon quality pilot v21

Campaign: `lhq-tb21-pilot-20261119-v21`

Plan SHA-256:
`aa95adf98769fa6a6d0653bafd3816753604ec5b6a154789dc15114c287574de`

Status: interrupted after 10 of 18 slots. This is an archive of observed
partial results, not a completed quality experiment.

## What completed

| Task | Arm | Started slots | Officially scored | Reward 1 | Requests |
|---|---|---:|---:|---:|---:|
| fix-git | React | 3 | 3 | 3 | 56 |
| fix-git | MEA | 3 | 2 | 2 | 158 |
| custom-memory-heap-crash | React | 1 | 0 | 0 | 37 |
| custom-memory-heap-crash | MEA | 3 | 1 | 0 | 150 |
| **Total** |  | **10** | **6** | **5** | **401** |

`large-scale-text-editing` did not start.

## Outcome split

- 6 scored slots
- 3 execution/orchestration errors
- 1 started slot interrupted without a final official result

The interrupted slot is
`lhq-cc81dd11fb02379ee7a6a7455a56fa0d`
(`custom-memory-heap-crash/react/t0`) after 37 settled
requests. Its run ID is closed and must not be replayed.

## Usage

- requests: 401
- pending requests: 0
- provider errors: 0
- unknown usage rows: 0
- prompt tokens: 554,258
- cache-read tokens: 1,935,744
- cache-write tokens: 0
- completion tokens: 400,477
- cache hit: 77.7407%
- fixed-price client estimate: `$1.227758`

## Technical result

The replacement OpenCode Go credential worked and no HTTP 429 recurred.
The dominant experiment-specific failure was the Auditor returning explanatory
text instead of the required JSON object after bounded repair. This consumed
substantial request budget in MEA and left several slots without an official
reward.

LongArc branding and later source fixes changed files covered by the frozen
source manifest. Therefore v21 cannot be resumed under its original plan. A
new campaign ID with fresh source pins is required.

## Claim boundary

This campaign does not establish that React or MEA is better. It cannot support
a benchmark-quality, long-horizon-task or cache-benefit claim.

The public machine summary is [summary.json](summary.json).
