# jev-pr-review

A GitHub Action that scores every changed file in a pull request with
[Jev](https://typesafe.ai) (TypeSafe System One) and posts a review comment.
**Shadow mode only right now**: it comments, it never merges anything.
Automerge is designed in but deliberately not reachable until there is real
calibration data to set safe thresholds.

## Why a standalone repo

The goal is "wire this into any repo's CI/CD", not "wire this into one repo".
This is a self-contained composite action with zero non-stdlib dependencies
-- consuming it is three lines of YAML.

## Use it in any repo

```yaml
- uses: ohernandezdev/jev-pr-review@v1
  with:
    pr-number: ${{ github.event.pull_request.number }}
    typesafe-api-key: ${{ secrets.TYPESAFE_API_KEY }}
```

Full workflow example:

```yaml
name: jev-pr-review
on:
  pull_request:                     # never pull_request_target, see Security below
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  checks: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: ohernandezdev/jev-pr-review@v1
        with:
          pr-number: ${{ github.event.pull_request.number }}
          typesafe-api-key: ${{ secrets.TYPESAFE_API_KEY }}
```

## Design: dimensions, not decisions

Jev scores **dimensions of the situation** (risk, whether the diff matches
the title, hidden scope, silent-failure risk, whether tests are expected).
The verdict -- `automerge` vs `escalate` -- is a plain Python decision made
from those scores plus hard gates. This split was verified live: asking Jev
directly "does this need human review" returned a useless ~0.46; asking it
"how much damage would this cause if wrong" as a dimensional score returned
2.97/3 at 0.97 confidence on the same diff.

One Jev request per changed file (not per PR): the `state` limit is 32k
tokens, and per-file scoring plus `max` aggregation keeps a single risky file
from being diluted by 39 trivial ones.

| Dimension | Type | What it judges |
|---|---|---|
| `risk_level` | score 0-3 | cosmetic / isolated logic / shared logic / auth-payments-data |
| `diff_matches_title` | noul | the change does what the PR title promises |
| `hidden_scope` | noul | the change does something ADDITIONAL beyond what the title says |
| `silent_failure` | noul | a mistake here would fail silently, not loudly |
| `touches_money` | noul | charges, payments, refunds, invoicing, pricing, balances |
| `touches_accounts` | noul | sign-in, passwords, tokens, permissions, visibility |
| `touches_personal_data` | noul | storage, export, deletion or exposure of personal data |
| `sensitive_area` | derived | the max of the three `touches_*` questions |
| `silent_failure_weighted` | derived | `silent_failure` scaled by that file's `risk_level` |
| `tests_expected` | noul | a competent reviewer would expect tests with this change |

## Aggregation and gates

Aggregation is `max` across files per dimension -- never an average, so one
dangerous file can't hide behind many trivial ones.

Hard gates and scored dimensions are **both always evaluated**, and all their
reasons are reported together. Any single failure forces `escalate`:

- a changed file matches `blocked_paths` (fnmatch)
- total changed lines (added + deleted) exceed `max_lines`
- CI is not green
- any dimension threshold in `automerge_when` is not met

Gates used to short-circuit and return before the dimensions were scored. The
table was then printed anyway, so the verdict's colour depended only on file
size and file name while the scores appeared to explain it. A reader who
notices that learns to ignore the table.

Jev API failures after retries are also fail-safe: they force `escalate`,
never `automerge`.

## Config

`.jev-review.yml` (or `.jev-review.json`, see below) in your repo root:

```yaml
mode: shadow

automerge_when:
  max_risk_level: "< 1.5"
  hidden_scope: "< 0.25"
  silent_failure_weighted: "< 0.30"
  diff_matches_title: "> 0.80"
  max_lines: 400

blocked_paths:
  - ".github/**"
  - "**/auth/**"
  - "**/migrations/**"
  - "**/*secret*"
  - "Dockerfile"
```

These starting thresholds are **not calibrated** -- they are launch values
from the design spec, meant to be tuned once real outcome data exists.

### YAML parsing note

This project deliberately does not depend on PyYAML (stdlib-only is a hard
requirement, see below). `.jev-review.yml` is parsed with a small hand-rolled
parser that only understands the flat subset used above: top-level scalars,
one level of nested scalar mapping (`automerge_when:`), and one flat list
(`blocked_paths:`). Anchors, multiline strings, and deeper nesting are out of
scope. If your config needs more than that, use `.jev-review.json` instead --
we chose robust over clever here.

### `mode`

Only `mode: shadow` is implemented. Setting `mode: enforce` fails loudly with
a clear message: there isn't yet calibration data to safely let this merge
anything. The merge code path is intentionally unwritten, not just disabled.

## Stdlib only, on purpose

`jev_pr_review.py` imports only `urllib.request`, `json`, `os`, `fnmatch`,
`argparse`, and `time`. No `requests`, no TypeSafe SDK. This has to run on a
bare `ubuntu-latest` runner with no install step, in any repo.

## Local usage

```bash
export TYPESAFE_API_KEY=...
export GITHUB_TOKEN=...
export GITHUB_REPOSITORY=owner/repo
python3 jev_pr_review.py --pr 123 --config .jev-review.yml

# Print the verdict without touching GitHub at all:
python3 jev_pr_review.py --pr 123 --dry-run
```

## Security

- Trigger on `pull_request`, **never** `pull_request_target`. `pull_request`
  runs with the base repo's read-only default token against untrusted PR
  code that is never checked out or executed.
- The script never checks out, builds, or executes anything from the PR --
  it only reads the diff and file list through the GitHub REST API.
- Minimum permissions: `pull-requests: write` (to comment), `contents: read`.

## Tests

```bash
pip install pytest
pytest tests/                    # unit tests only, no network
pytest tests/ -m e2e             # includes the real Jev API E2E test
```

The E2E test is skipped automatically if `TYPESAFE_API_KEY` is not set. It
does **not** mock the API -- it sends two real diffs (an auth change, a
README typo fix) and asserts the auth one scores higher risk and gets a
stricter verdict.

### Why `silent_failure` is never used raw

Measured on real diffs: a README typo scores `silent_failure` **0.57**, while
swapping `jwt.decode` for `jwt.verify` scores **0.20**. Jev is right both times --
a typo warns nobody, and `jwt.verify` throws loudly. The dimension is sound; using
it alone is not. A silent failure only matters when there is something to damage,
so it is scaled by that file's `risk_level` before any threshold sees it:

| diff | risk | silent | weighted |
|---|---|---|---|
| README typo | 0.00 | 0.57 | **0.00** |
| `jwt.decode` -> `jwt.verify` | 3.00 | 0.20 | **0.20** |
| add retry/backoff | 1.86 | 0.71 | **0.44** |
| `except: return None` around a charge | 2.98 | 0.92 | **0.91** |

The pairing happens per file, before the max across files -- otherwise the README's
silence would pair with the auth file's risk.

### When enforce mode arrives, it will not merge by itself

The reviewer produces a semantic verdict. It should never be the thing that
decides CI was green: it is one check among others, it starts alongside them,
and any snapshot it takes is a race it can lose. The CI gate belongs to branch
protection, and the merge to `gh pr merge --auto`, which GitHub holds until the
required checks pass. This action's job ends at the verdict.

The bounded wait in `await_ci_status` exists only so the shadow-mode data
records what CI actually did, rather than what was true in the first second.

### The comment is written for whoever decides, not for whoever wrote the code

Probabilities appear as percentages, each dimension is a question in plain
words, the risk level is a word with its meaning spelled out, and the wording
carries the direction so the number only confirms it. The raw scores stay in a
folded `<details>` block for whoever wants them.

The comment was then red-teamed by a non-technical reader -- an operations
lead, the person who would actually be asked to sign off. What that changed:

- **The table participates in the verdict.** See "Aggregation and gates" above.
- **Three answer bands, and the middle one says so.** Below 10% is `No`, above
  90% is `Yes`, and everything between is `Not confident either way`. A green
  tick needs both a reassuring direction *and* certainty, so nothing in the
  middle band is ever ticked. `Probably not (38%)` beside a tick was reading as
  an all-clear; a shrug has to look like a shrug.
- **One question about the title, not two.** "Does it do what the title says?"
  and "Does it also change things the title doesn't mention?" read as the same
  question asked twice, so they render as one row: *Does the title describe the
  whole change?* Both dimensions are still asked of Jev, still kept in the raw
  scores, and still have their own thresholds. When the answer is
  `No -- it changes more than it says`, that finding is promoted to the first
  line of **Why** instead of sitting in table-row order.
- **Tests are a fact, not an opinion.** The row is *Does it include tests?*,
  answered from `has_test_changes`, which we already compute. `tests_expected`
  only qualifies the no: `No -- and a reviewer would expect them`.
- **Money, accounts and personal data are three questions.** One aggregate
  number hid which kind of trouble it was. They are asked separately, gated on
  their max (`sensitive_area`), and the row names the ones that fired.
- **The comment says which PR it is.** Title, author, files and lines changed
  sit under the headline of both the red and the green comment -- nobody can
  sign off on a change they cannot identify.
- **An escalation can name an owner.** Optional `escalate_to` appends
  ` -- assigned to: @who` to the red headline. Unset, nothing is invented.
- **The cost moved inside the fold.** A cost quoted next to the verdict reads
  as "this was cheap, therefore it was shallow". Outside stays the file count.

The percentages themselves stay: they were the one thing the red-team wanted
removed, and they are deliberately kept.

### Why the mean risk is not enough

`risk_level` is a probability-weighted mean over the levels, so it compensates.
A file split 50/50 between "cosmetic" and "logins, payments or data loss"
scores exactly 1.5 and slips under a `< 1.5` threshold with half its
probability mass on catastrophe. Measured distributions behind real scores:

| diff | score | distribution |
|---|---|---|
| add retry/backoff | 1.85 | 15% level 1, 85% level 2 |
| `jwt.decode` -> `jwt.verify` | 3.00 | 100% level 3 |

So `worst_case_risk` -- the probability mass on the worst level -- gets its own
threshold. A mean answers "how bad on average", and nothing about merging
without a person is an average question. A missing distribution is absent, not
zero: the gate then has no value and escalates.
