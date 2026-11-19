# Long-horizon quality pilot v20

Campaign: `lhq-tb21-pilot-20261119-v20`

Plan SHA-256:
`dc193444562c953df73f5b2f4da306da7147aa7a219181e2e76ca09086e44158`

Status: all 18 slots reached a terminal journal state, but this remains a
three-task descriptive pilot. It does not support a React-versus-MEA quality
claim.

## Result

| Arm | Slots | Official scored | Reward 1 | Requests |
|---|---:|---:|---:|---:|
| React | 9 | 7 | 2 | 307 |
| MEA | 9 | 1 | 1 | 494 |
| **Total** | **18** | **8** | **3** | **801** |

| Task | Arm | Reward 1 / 3 | Scored / 3 | Requests |
|---|---|---:|---:|---:|
| fix-git | React | 2 | 3 | 58 |
| fix-git | MEA | 1 | 1 | 152 |
| custom-memory-heap-crash | React | 0 | 3 | 159 |
| custom-memory-heap-crash | MEA | 0 | 0 | 186 |
| large-scale-text-editing | React | 0 | 1 | 90 |
| large-scale-text-editing | MEA | 0 | 0 | 156 |

The 18 slots split into 8 `scored` outcomes and 10
`execution_or_verifier_error` outcomes. Because the arms do not share the same
set of scored tasks, the pilot produced no valid paired task outcome.

## Usage

- net prompt tokens: 1,005,601
- cache-read tokens: 4,578,688
- cache-write tokens: 0
- completion tokens: 788,835
- cache hit, `cache_read / (prompt + cache_read + cache_write)`: 81.9923%
- known client-side estimate: `$2.622209`
- unknown usage slots: 3

The client cost is a fixed-price estimate, not supplier billing.

## Interpretation

- MEA spent more requests and had fewer officially scored outcomes.
- React and MEA did not produce a comparable paired score distribution.
- A nonzero reward count cannot be used as a quality ranking.
- Timeouts, orchestration errors and absent verifier reports remain in the
  archive and were not converted into failures at the task level.

The public machine summary is [summary.json](summary.json).
