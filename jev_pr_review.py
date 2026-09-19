#!/usr/bin/env python3
"""jev-pr-review: shadow-mode PR review scored by Jev (TypeSafe System One).

Stdlib only, on purpose: this script must run in any GitHub Actions runner
without an install step. Pure decision logic (aggregation, gates, truncation,
verdict) is split from I/O (Jev HTTP calls, GitHub API calls) so it can be
unit tested without network access.

Design principle (verified against the live API on 2026-09-19): Jev scores
dimensions of the SITUATION, never policy decisions. Asking "does this need
human review" directly returns noise (~0.46); asking a dimensional question
like "how much damage would this cause if wrong" returns a sharp, confident
score. Question instructions below must never mention merge, approval,
review, or the verdict name -- only describe the situation and consequences.
The threshold and aggregation live here, in Python.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-latest"
COST_PER_MILLION_INPUT_TOKENS = 0.042  # USD, for the cost line in the PR comment
COMMENT_MARKER = "<!-- jev-pr-review -->"
CHARS_PER_TOKEN = 4  # rough estimate, matches the spec's "4 chars/token"
MAX_DIFF_TOKENS = 24_000

# --- The five dimensional questions -----------------------------------------
# Each `instructions` string describes the situation and its consequences.
# None of them mention "merge", "approve", "review", or a verdict name --
# that is Jev's job to never see, and Python's job to decide.
QUESTIONS: dict[str, dict[str, Any]] = {
    "risk_level": {
        "type": "score",
        "instructions": (
            "A file inside a software change is described below: its diff, "
            "lines added/removed, and the PR title/body giving intent. Judge "
            "how much damage this file's change could cause if it turns out "
            "to be wrong -- from a cosmetic difference nobody would notice, "
            "up to a defect that could leak, corrupt, or lose money, "
            "credentials, or user data."
        ),
        "criteria": [
            "cosmetic or purely presentational change with no behavioral effect",
            "isolated logic change contained to this file, low blast radius",
            "logic change that is shared or depended on by other parts of the system",
            "touches authentication, payments, secrets, or user data directly",
        ],
    },
    "diff_matches_title": {
        "type": "noul",
        "instructions": (
            "A file's diff and the PR title/body describing the intended "
            "change are given below. Judge whether this file's actual change "
            "does what the title and body say it does."
        ),
        "criteria": {
            "true": "the file's diff is consistent with and implements what the title/body describe",
            "false": "the file's diff does something different from, or unrelated to, what the title/body describe",
        },
    },
    "hidden_scope": {
        "type": "noul",
        "instructions": (
            "A file's diff and the PR title/body describing the intended "
            "change are given below. Judge whether this file's diff does "
            "something ADDITIONAL that the title/body never mention -- an "
            "extra behavior change riding along with the described one."
        ),
        "criteria": {
            "true": "the diff includes a change beyond what the title/body describe",
            "false": "the diff stays within what the title/body describe, nothing extra",
        },
    },
    "silent_failure": {
        "type": "noul",
        "instructions": (
            "A file's diff is given below. Judge whether a mistake in this "
            "code would fail silently -- producing wrong results, swallowed "
            "errors, or no visible signal -- rather than failing loudly with "
            "an exception, a crash, or an obvious error message."
        ),
        "criteria": {
            "true": "a defect here would likely go unnoticed, no loud signal",
            "false": "a defect here would likely surface immediately and loudly",
        },
    },
    # Red-team finding: one aggregate "does it touch logins, payments or data"
    # mixes three different kinds of trouble. If the answer is not a flat no,
    # the reader needs to know WHICH, so each one is its own question.
    "touches_money": {
        "type": "noul",
        "instructions": (
            "A file's diff and the PR title/body describing the intended "
            "change are given below. Judge whether this change affects how "
            "money moves: charges, payments, refunds, invoicing, pricing, "
            "or account balances."
        ),
        "criteria": {
            "true": "the change affects charging, payments, billing, pricing, or balances",
            "false": "the change has nothing to do with money",
        },
    },
    "touches_accounts": {
        "type": "noul",
        "instructions": (
            "A file's diff and the PR title/body describing the intended "
            "change are given below. Judge whether this change affects who "
            "can get in and what they can reach: sign-in, passwords, tokens, "
            "sessions, permissions, or who is allowed to see what."
        ),
        "criteria": {
            "true": "the change affects sign-in, credentials, tokens, permissions, or visibility rules",
            "false": "the change has nothing to do with accounts or access",
        },
    },
    "touches_personal_data": {
        "type": "noul",
        "instructions": (
            "A file's diff and the PR title/body describing the intended "
            "change are given below. Judge whether this change affects "
            "customers' personal data: how it is stored, exported, deleted, "
            "or exposed to anyone."
        ),
        "criteria": {
            "true": "the change affects storage, export, deletion, or exposure of personal data",
            "false": "the change does not involve customers' personal data",
        },
    },
    "tests_expected": {
        "type": "noul",
        "instructions": (
            "A file's diff, its lines added/removed, and whether the overall "
            "change already includes test changes are given below. Judge "
            "whether a competent engineer looking only at this file's diff "
            "would expect automated tests to accompany a change like this."
        ),
        "criteria": {
            "true": "a competent engineer would expect tests for a change like this",
            "false": "this kind of change would not normally need its own tests",
        },
    },
}


# --- Pure functions ----------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 4 chars/token, per spec."""
    return len(text) // CHARS_PER_TOKEN


def truncate_diff(diff: str, max_tokens: int = MAX_DIFF_TOKENS) -> tuple[str, bool]:
    """Truncate a diff by the middle, keeping head and tail, if it is too long.

    Returns (possibly-truncated diff, was_truncated).
    """
    if estimate_tokens(diff) <= max_tokens:
        return diff, False

    max_chars = max_tokens * CHARS_PER_TOKEN
    marker = "\n\n... [diff truncated by jev-pr-review, middle section omitted] ...\n\n"
    budget = max_chars - len(marker)
    if budget <= 0:
        # Degenerate case: max_tokens too small for even the marker.
        return diff[:max_chars], True

    head_len = budget // 2
    tail_len = budget - head_len
    truncated = diff[:head_len] + marker + diff[len(diff) - tail_len :]
    return truncated, True


def build_file_state(
    *,
    pr_title: str,
    pr_body: str,
    file_path: str,
    diff: str,
    lines_added: int,
    lines_deleted: int,
    has_test_changes: bool,
    ci_status: str,
) -> dict[str, Any]:
    """Build the per-file `state` payload sent to Jev, truncating the diff."""
    truncated_diff, was_truncated = truncate_diff(diff)
    state: dict[str, Any] = {
        "pr_title": pr_title,
        "pr_body": pr_body,
        "file_path": file_path,
        "diff": truncated_diff,
        "lines_added": lines_added,
        "lines_deleted": lines_deleted,
        "has_test_changes": has_test_changes,
        "ci_status": ci_status,
    }
    if was_truncated:
        state["diff_truncated"] = True
    return state


# Highest level index of the `risk_level` score, used to normalise it to 0..1.
RISK_LEVEL_MAX = len(QUESTIONS["risk_level"]["criteria"]) - 1

# The three separate sensitive areas, and the short words used to name them to
# a reader. `sensitive_area` is their max, and is what the gate threshold sees.
SENSITIVE_AREAS = {
    "touches_money": "money",
    "touches_accounts": "accounts",
    "touches_personal_data": "personal data",
}

# Above this a probability is treated as a settled "yes", below its complement
# as a settled "no". Red-team rule: anything in between is a shrug, and a shrug
# never gets a green tick -- a 38% with a check mark is what broke trust in the
# table in the first place.
CERTAIN = 0.90
CERTAINLY_NOT = 0.10
# Below this a signal is noise, not something to name in a warning.
NAMEABLE = 0.50


def aggregate_max(per_file_answers: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate per-file Jev answers into one score per dimension via max.

    `per_file_answers` is a list of `answers` dicts as returned by the Jev
    client (one per reviewed file). Missing dimensions in a file are skipped
    for that file. Returns {} if there are no files.
    """
    result: dict[str, float] = {}
    for answers in per_file_answers:
        per_file: dict[str, float] = {}
        for dim, answer in answers.items():
            value = answer.get("score", answer.get("noul"))
            if value is not None:
                per_file[dim] = value
            if dim == "risk_level":
                tail = top_level_probability(answer)
                if tail is not None:
                    per_file["worst_case_risk"] = tail
        derived = derive_dimensions(per_file)
        for dim, value in derived.items():
            if dim not in result or value > result[dim]:
                result[dim] = value
    return result


def top_level_probability(answer: dict[str, Any]) -> Optional[float]:
    """P(the highest risk level) from a score answer's distribution.

    The score itself is a probability-weighted mean, so it compensates: a file
    split 50/50 between "cosmetic" and "logins, payments or data loss" scores
    exactly 1.5 and slips under a `< 1.5` mean threshold, with half its
    probability mass saying catastrophe. A mean answers "how bad on average";
    nothing about automerge is an average question, so the tail gets its own
    condition.
    """
    probabilities = answer.get("probabilities") or {}
    if not probabilities:
        # Absent is not zero. A missing distribution leaves the gate without a
        # value, which escalates, instead of reading as a reassuring 0%.
        return None
    top = max(probabilities, key=lambda level: int(level))
    return float(probabilities[top])


def derive_dimensions(per_file: dict[str, float]) -> dict[str, float]:
    """Add dimensions computed from a single file's raw scores.

    `silent_failure` is meaningless on its own: a README typo scores higher
    than a JWT fix, because a typo warns nobody while `jwt.verify` throws
    loudly. Measured on real diffs: README 0.57 vs auth 0.20. Scaling it by
    that same file's risk restores the intended ordering, so the pairing must
    happen per file -- before the max across files, never after.
    """
    out = dict(per_file)
    risk = per_file.get("risk_level")
    silent = per_file.get("silent_failure")
    if risk is not None and silent is not None:
        out["silent_failure_weighted"] = silent * (risk / RISK_LEVEL_MAX)
    present = [per_file[d] for d in SENSITIVE_AREAS if per_file.get(d) is not None]
    if present:
        out["sensitive_area"] = max(present)
    return out


def evaluate_hard_gates(
    *,
    files_changed: list[str],
    total_lines_changed: int,
    ci_status: str,
    blocked_paths: list[str],
    max_lines: int,
) -> list[str]:
    """Evaluate hard gates before any probability is consulted.

    Returns a list of human-readable failure reasons; empty means all gates
    passed. Any non-empty result forces `escalate` regardless of scores.
    """
    reasons: list[str] = []

    matched_blocked = sorted(
        {
            f
            for f in files_changed
            for pattern in blocked_paths
            if fnmatch.fnmatch(f, pattern)
        }
    )
    if matched_blocked:
        reasons.append({"code": "blocked_path", "paths": matched_blocked})

    if total_lines_changed > max_lines:
        reasons.append({"code": "too_large", "lines": total_lines_changed, "limit": max_lines})

    if ci_status == "unreadable":
        reasons.append({"code": "ci_unreadable"})
    elif ci_status.strip().lower() not in {"all checks passing", "success", "passing"}:
        reasons.append({"code": "ci_not_green", "status": ci_status})

    return reasons


def parse_threshold(expr: str) -> tuple[str, float]:
    """Parse a threshold expression like '< 1.5' or '> 0.80' into (op, value)."""
    expr = expr.strip()
    for op in ("<=", ">=", "<", ">", "==", "="):
        if expr.startswith(op):
            return op, float(expr[len(op) :].strip())
    raise ValueError(f"Unrecognized threshold expression: {expr!r}")


def check_threshold(value: float, expr: str) -> bool:
    op, target = parse_threshold(expr)
    if op == "<":
        return value < target
    if op == "<=":
        return value <= target
    if op == ">":
        return value > target
    if op == ">=":
        return value >= target
    if op in ("==", "="):
        return value == target
    raise ValueError(f"Unrecognized operator: {op!r}")


def decide_verdict(
    *,
    aggregated: dict[str, float],
    files_changed: list[str],
    total_lines_changed: int,
    ci_status: str,
    config: dict[str, Any],
    network_failure: bool = False,
    has_test_changes: Optional[bool] = None,
) -> tuple[str, list[str]]:
    """Compute the final verdict and its reasons.

    Fail-safe: any network failure after retries forces `escalate`, never
    `automerge`, regardless of everything else.

    Hard gates and dimensions are BOTH always evaluated, and all their reasons
    are returned together. Returning early on a gate made the scored table
    decorative -- the verdict's colour then depended only on file size and file
    name, which trains a reader to ignore the table.
    """
    if network_failure:
        return "escalate", [{"code": "review_unreachable"}]

    gate_reasons = evaluate_hard_gates(
        files_changed=files_changed,
        total_lines_changed=total_lines_changed,
        ci_status=ci_status,
        blocked_paths=config.get("blocked_paths", []),
        max_lines=config.get("automerge_when", {}).get("max_lines", 400),
    )
    thresholds = config.get("automerge_when", {})
    dim_reasons: list[dict[str, Any]] = []
    dimension_thresholds = {
        "risk_level": thresholds.get("max_risk_level"),
        "hidden_scope": thresholds.get("hidden_scope"),
        "silent_failure_weighted": thresholds.get("silent_failure_weighted"),
        "worst_case_risk": thresholds.get("worst_case_risk"),
        "diff_matches_title": thresholds.get("diff_matches_title"),
        "sensitive_area": thresholds.get("sensitive_area"),
    }
    for dim, expr in dimension_thresholds.items():
        if expr is None:
            continue
        value = aggregated.get(dim)
        if value is None:
            dim_reasons.append({"code": "no_score", "dimension": dim})
            continue
        if check_threshold(value, expr):
            continue
        if dim == "sensitive_area":
            # Name the areas rather than the aggregate: "it touches money" and
            # "it touches personal data" are not the same warning.
            # Name only the areas actually driving the signal. Listing every
            # area above 0.10 put "personal data" in a warning when no file
            # scored over 0.10 on it -- a false alarm in the one line the
            # reader trusts most.
            areas = [
                name
                for key, name in SENSITIVE_AREAS.items()
                if (aggregated.get(key) or 0.0) > NAMEABLE
            ]
            # Only claim it outright when the row does too; below that the row
            # says "not confident either way" and so must this line.
            dim_reasons.append({
                "code": "sensitive_area",
                "areas": areas,
                "value": value,
                "confident": value > CERTAIN,
            })
        else:
            dim_reasons.append({"code": "dimension", "dimension": dim, "value": value})

    if has_test_changes is False and (aggregated.get("tests_expected") or 0.0) > CERTAIN:
        dim_reasons.append({"code": "tests_missing_but_expected"})

    # "It changes more than its title says" is the finding a non-engineer acts
    # on first, so it leads the list instead of sitting in table-row order.
    lead: list[dict[str, Any]] = []
    for i, reason in enumerate(dim_reasons):
        if reason.get("dimension") == "hidden_scope" and reason["value"] > CERTAIN:
            lead.append({"code": "changes_more_than_title", "value": dim_reasons.pop(i)["value"]})
            break

    reasons = lead + gate_reasons + dim_reasons
    if reasons:
        return "escalate", reasons
    return "automerge", [{"code": "all_clear"}]


# --- Jev client ---------------------------------------------------------------


class JevClientError(Exception):
    """Raised when the Jev API is unreachable after retries."""


def call_jev(
    state: dict[str, Any],
    questions: dict[str, Any],
    *,
    api_key: Optional[str] = None,
    max_retries: int = 3,
    timeout: float = 30.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Call the TypeSafe System One API and return `answers`.

    Retries with exponential backoff on HTTP 429/529, honoring Retry-After
    when present. Raises JevClientError after exhausting retries -- callers
    must treat that as fail-safe escalate, never automerge.
    """
    api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise JevClientError("TYPESAFE_API_KEY is not set")

    payload = json.dumps(
        {"model": TYPESAFE_MODEL, "state": state, "questions": questions}
    ).encode("utf-8")

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        request = urllib.request.Request(
            TYPESAFE_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
                return body
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (429, 529) and attempt < max_retries - 1:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = float(retry_after) if retry_after else (2**attempt)
                sleep_fn(delay)
                continue
            raise JevClientError(f"Jev API HTTP error: {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < max_retries - 1:
                sleep_fn(2**attempt)
                continue
            raise JevClientError(f"Jev API unreachable: {exc}") from exc

    raise JevClientError(f"Jev API unreachable after {max_retries} retries: {last_error}")


# --- GitHub I/O ---------------------------------------------------------------


def _github_request(
    url: str, *, token: str, method: str = "GET", body: Optional[dict[str, Any]] = None
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30.0) as response:
        raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else None


def fetch_pr(repo: str, pr_number: int, token: str) -> dict[str, Any]:
    return _github_request(f"https://api.github.com/repos/{repo}/pulls/{pr_number}", token=token)


def fetch_pr_files(repo: str, pr_number: int, token: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    page = 1
    while True:
        page_files = _github_request(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
            f"?per_page=100&page={page}",
            token=token,
        )
        if not page_files:
            break
        files.extend(page_files)
        if len(page_files) < 100:
            break
        page += 1
    return files


def compute_ci_status(check_runs: list[dict[str, Any]], *, own_run_id: str = "") -> str:
    """Derive a CI verdict from a commit's check runs, ignoring our own.

    `mergeable_state` cannot be used here: this reviewer is itself a check on
    the pull request, so while it runs the state is `unstable` and it would
    never observe a green CI -- it would block every pull request on its own
    existence. A run whose URL carries `own_run_id` is therefore dropped.

    Returns "success", "pending", "failure", or "no checks". A repository with
    no other checks is not green: nothing has vouched for the change.
    """
    others = [
        run
        for run in check_runs
        if not (own_run_id and f"/runs/{own_run_id}/" in (run.get("html_url") or ""))
    ]
    if not others:
        return "no checks"
    if any(run.get("status") != "completed" for run in others):
        return "pending"
    ok = {"success", "neutral", "skipped"}
    if any((run.get("conclusion") or "") not in ok for run in others):
        return "failure"
    return "success"


class CheckRunsUnreadable(Exception):
    """The check-runs endpoint could not be read, so CI state is unknown."""


def fetch_check_runs(repo: str, sha: str, token: Optional[str]) -> list[dict[str, Any]]:
    """Check runs for a commit.

    An unreadable list is NOT an empty list. Swallowing the error here once
    turned a missing `checks: read` permission into a confident "no checks",
    which looks exactly like a repository that has no CI. The caller has to be
    able to tell those apart, so the failure is raised.
    """
    url = f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100"
    try:
        payload = _github_request(url, token=token)
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        # Only transport failures are caught. A TypeError here once masqueraded
        # as a permissions problem for a whole debugging session, so anything
        # that is not the network is left to crash loudly.
        raise CheckRunsUnreadable(f"cannot read check runs for {sha[:7]}: {exc}") from exc
    return (payload or {}).get("check_runs", []) or []


def await_ci_status(
    repo: str,
    sha: str,
    token: Optional[str],
    *,
    own_run_id: str = "",
    timeout_s: float = 300.0,
    poll_s: float = 15.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
) -> str:
    """Wait, bounded, for the other checks on `sha` to settle.

    This job starts at the same time as the repository's other workflows, so a
    single snapshot usually finds them queued or not yet registered and reports
    "no checks". Every pull request would then escalate on a race, and the
    shadow-mode data collected to calibrate thresholds would be worthless.

    Polling only makes the observation honest. It is not how automerge should
    gate on CI: that belongs to branch protection plus `--auto`, which holds
    the merge itself instead of trusting one snapshot taken by this process.
    """
    if not sha:
        return "no checks"
    def snapshot() -> str:
        try:
            runs = fetch_check_runs(repo, sha, token)
        except CheckRunsUnreadable:
            return "unreadable"
        return compute_ci_status(runs, own_run_id=own_run_id)

    deadline = now_fn() + timeout_s
    status = snapshot()
    while status in {"pending", "no checks"} and now_fn() < deadline:
        sleep_fn(poll_s)
        status = snapshot()
    return status


def usable_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out binary files and files without a text `patch`."""
    return [f for f in files if f.get("patch")]


def find_existing_comment(repo: str, pr_number: int, token: str) -> Optional[int]:
    comments = _github_request(
        f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments?per_page=100",
        token=token,
    )
    for comment in comments or []:
        if COMMENT_MARKER in (comment.get("body") or ""):
            return comment["id"]
    return None


def upsert_comment(repo: str, pr_number: int, token: str, body: str) -> None:
    existing_id = find_existing_comment(repo, pr_number, token)
    full_body = f"{COMMENT_MARKER}\n{body}"
    if existing_id is not None:
        _github_request(
            f"https://api.github.com/repos/{repo}/issues/comments/{existing_id}",
            token=token,
            method="PATCH",
            body={"body": full_body},
        )
    else:
        _github_request(
            f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
            token=token,
            method="POST",
            body={"body": full_body},
        )


# --- Comment rendering ---------------------------------------------------------


# --- Plain-language rendering --------------------------------------------------
#
# The comment is read by whoever is deciding whether to merge, and that person is
# not always the one who wrote the code. Probabilities are shown as percentages,
# every dimension is phrased as a question in plain words, and the raw numbers
# stay available but folded away.

LINE_SEP = chr(10)

QUESTION_TEXT = {
    # One row, not two: "does it do what the title says" and "does it also do
    # something the title doesn't mention" read to a non-engineer as the same
    # question asked twice. Both are still asked of Jev -- only the row merged.
    "title_scope": "Does the title describe the whole change?",
    "silent_failure_weighted": "Could a mistake here break things quietly?",
    "sensitive_area": "Does it touch money, accounts or personal data?",
    "has_tests": "Does it include tests?",
    # Used when a dimension has to be named in a reason sentence.
    "diff_matches_title": "Does the change do what its title says?",
    "hidden_scope": "Does it also change things its title doesn't mention?",
    # Not "does it touch logins, payments or data" any more -- that is now its
    # own row, asked directly. This one is the tail of the risk distribution.
    "worst_case_risk": "If it is wrong, how likely is the worst case?",
    "tests_expected": "Would a reviewer expect tests with this?",
}

# True when a high percentage is the reassuring answer.
HIGH_IS_GOOD = {
    "diff_matches_title": True,
    "hidden_scope": False,
    "silent_failure_weighted": False,
    "worst_case_risk": False,
    "sensitive_area": False,
    "tests_expected": False,
}

RISK_BANDS = [
    (0.5, "None", "cosmetic only: docs, comments, formatting"),
    (1.5, "Low", "isolated logic, and a mistake shows up immediately"),
    (2.5, "Medium", "shared logic that several features rely on"),
    (float("inf"), "High", "logins, payments, migrations or data loss"),
]


def as_percent(value: float) -> str:
    return f"{round(value * 100)}%"


def risk_label(score: float) -> tuple[str, str, str]:
    """(icon, word, explanation) for a 0..3 risk score."""
    for ceiling, word, explanation in RISK_BANDS:
        if score < ceiling:
            icon = {"None": "🟢", "Low": "🟢", "Medium": "🟠", "High": "🔴"}[word]
            return icon, word, explanation
    raise AssertionError("unreachable")  # pragma: no cover


# A percentage alone reads as "how sure are you of that answer", which is not
# what it means, so the wording carries the direction. There are exactly three
# bands and the middle one says so out loud: "Probably not (38%)" next to a
# green tick reads as an all-clear, which a 38% is not. A shrug has to look
# like a shrug.
UNSURE = "Not confident either way"


def answer_label(dimension: str, value: float) -> tuple[str, str]:
    """(icon, wording) for a probability, read in the direction that matters.

    A green tick needs both a reassuring direction AND certainty: nothing
    between 10% and 90% is ever ticked.
    """
    if value < CERTAINLY_NOT:
        wording, says_yes = "No", False
    elif value > CERTAIN:
        wording, says_yes = "Yes", True
    else:
        return "⚠️", UNSURE

    high_is_good = HIGH_IS_GOOD.get(dimension, True)
    reassuring = says_yes == high_is_good
    icon = "✅" if reassuring else "⚠️"
    return icon, wording


CHANGES_MORE_THAN_TITLE = "No -- it changes more than it says"


def title_scope_label(
    diff_matches_title: Optional[float], hidden_scope: Optional[float]
) -> tuple[str, str]:
    """(icon, wording) for the merged title row.

    Yes only when the change does what the title says AND adds nothing the
    title omits. A confident "it does more than it says" is the loud case.
    """
    matches = diff_matches_title if diff_matches_title is not None else 0.0
    hidden = hidden_scope if hidden_scope is not None else 1.0
    if matches > CERTAIN and hidden < CERTAINLY_NOT:
        return "✅", "Yes"
    if hidden > CERTAIN:
        return "⚠️", CHANGES_MORE_THAN_TITLE
    return "⚠️", UNSURE


def tests_label(has_tests: bool, tests_expected: Optional[float]) -> tuple[str, str]:
    """(icon, wording) for the tests row.

    Whether tests are present is a fact we already computed, so the row states
    it. `tests_expected` is an opinion, and only qualifies the "no".
    """
    if has_tests:
        return "✅", "Yes"
    if tests_expected is not None and tests_expected > CERTAIN:
        return "⚠️", "No -- and a reviewer would expect them"
    return "⚠️", "No"


def sensitive_label(aggregated: dict[str, float]) -> tuple[str, str]:
    """(icon, wording) for the money/accounts/personal-data row."""
    values = {k: aggregated.get(k) for k in SENSITIVE_AREAS}
    present = [v for v in values.values() if v is not None]
    if not present:
        return "⚠️", UNSURE
    hits = [name for key, name in SENSITIVE_AREAS.items() if (values[key] or 0.0) > CERTAIN]
    if hits:
        return "⚠️", f"Yes -- {', '.join(hits)}"
    if all(v < CERTAINLY_NOT for v in present):
        return "✅", "No"
    return "⚠️", UNSURE


def humanize_reason(reason: dict[str, Any]) -> str:
    """One sentence a non-engineer can act on."""
    code = reason.get("code")
    if code == "blocked_path":
        paths = ", ".join(f"`{p}`" for p in reason["paths"])
        return f"It changes files that always need a person: {paths}"
    if code == "too_large":
        return (
            f"It is large: {reason['lines']} lines changed, and the limit for "
            f"merging without a person is {reason['limit']}"
        )
    if code == "ci_unreadable":
        return (
            "The project's test results could not be read, so nothing is "
            "assumed to be passing"
        )
    if code == "ci_not_green":
        status = reason.get("status", "")
        if status == "pending":
            return "The project's own tests have not finished running yet"
        if status == "failure":
            return "The project's own tests are failing"
        if status == "no checks":
            return "This project has no automated tests to vouch for the change"
        return f"The project's own tests are not passing (status: {status})"
    if code == "review_unreachable":
        return (
            "The review service could not be reached, so nothing is assumed "
            "to be safe"
        )
    if code == "no_score":
        return f"One check did not come back with an answer ({reason['dimension']})"
    if code == "dimension":
        dim, value = reason["dimension"], reason["value"]
        if dim == "risk_level":
            _, word, explanation = risk_label(value)
            return f"If this change is wrong the damage is {word.lower()}: {explanation}"
        # A reason has to be a statement. Echoing the table's question here read
        # as a second, unanswered prompt, and a reason phrased more confidently
        # than its own row is the contradiction the red-team called poisonous.
        statements = {
            "silent_failure_weighted": "a mistake here might go unnoticed",
            "worst_case_risk": "the worst case cannot be ruled out",
            "hidden_scope": "it may change more than its title says",
            "diff_matches_title": "it may not do what its title says",
            "sensitive_area": "it may touch money, accounts or personal data",
        }
        statement = statements.get(dim)
        if statement is None:
            return f"One check did not come back clear ({as_percent(value)})"
        return f"{statement.capitalize()} ({as_percent(value)})"
    if code == "changes_more_than_title":
        return (
            "It changes more than its title says: "
            f"{as_percent(reason['value'])} likely there is an extra change "
            "riding along with the described one"
        )
    if code == "sensitive_area":
        areas = reason.get("areas") or []
        listed = ", ".join(areas) if areas else "money, accounts or personal data"
        confident = reason.get("confident", True)
        verb = "It touches" if confident else "It may touch"
        return f"{verb} {listed}"
    if code == "tests_missing_but_expected":
        return "It comes with no tests, and a change like this would normally have them"
    if code == "all_clear":
        return "Every check came back clear"
    # A code with no sentence is a bug, but the person reading this did not
    # write it and must not be shown a dict. Say the honest thing instead.
    return "Something the review flagged could not be explained here -- see the technical detail"


def render_comment(
    *,
    verdict: str,
    reasons: list[dict[str, Any]],
    aggregated: dict[str, float],
    total_input_tokens: int,
    files_reviewed: int = 0,
    pr_title: str = "",
    pr_author: str = "",
    files_changed: Optional[int] = None,
    lines_changed: Optional[int] = None,
    has_test_changes: Optional[bool] = None,
    escalate_to: str = "",
) -> str:
    cost_usd = total_input_tokens * COST_PER_MILLION_INPUT_TOKENS / 1_000_000

    if verdict == "automerge":
        headline = "🟢 **This looks safe to merge without a review**"
    else:
        headline = "🔴 **Someone should look at this before it is merged**"
        # A warning with no addressee is a warning nobody owns.
        if escalate_to:
            headline += f" -- assigned to: {escalate_to}"

    lines = ["## Review summary", "", headline, ""]

    # Nobody can sign off on a change they cannot identify, so the comment says
    # which change it is before it says anything about it.
    facts = []
    if pr_author:
        facts.append(f"by {pr_author}")
    if files_changed is not None:
        facts.append(f"{files_changed} file{'' if files_changed == 1 else 's'} changed")
    if lines_changed is not None:
        facts.append(f"{lines_changed} line{'' if lines_changed == 1 else 's'} changed")
    if pr_title or facts:
        if pr_title:
            lines.append(f"> **{pr_title}**")
        if facts:
            lines.append(f"> {' · '.join(facts)}")
        lines.append("")

    if verdict != "automerge" or reasons:
        # The title question renders as one row, so it is one line in "Why"
        # too: the lead already says the change does more than its title.
        shown = reasons
        if any(r.get("code") == "changes_more_than_title" for r in reasons):
            shown = [r for r in reasons if r.get("dimension") != "diff_matches_title"]
        lines.append("**Why**")
        lines += [f"- {humanize_reason(r)}" for r in shown]
        lines.append("")

    lines += [
        "**What the automatic review found**",
        "",
        "| Question | Answer |",
        "|---|---|",
    ]

    risk = aggregated.get("risk_level")
    if risk is not None:
        icon, word, explanation = risk_label(risk)
        lines.append(f"| If this change is wrong, how bad is it? | {icon} **{word}** -- {explanation} |")

    matches, hidden = aggregated.get("diff_matches_title"), aggregated.get("hidden_scope")
    if matches is not None or hidden is not None:
        icon, word = title_scope_label(matches, hidden)
        parts = []
        if matches is not None:
            parts.append(f"does what it says {as_percent(matches)}")
        if hidden is not None:
            parts.append(f"extra changes {as_percent(hidden)}")
        lines.append(
            f"| {QUESTION_TEXT['title_scope']} | {icon} **{word}** ({', '.join(parts)}) |"
        )

    sensitive = aggregated.get("sensitive_area")
    if sensitive is not None:
        icon, word = sensitive_label(aggregated)
        lines.append(
            f"| {QUESTION_TEXT['sensitive_area']} | {icon} **{word}** ({as_percent(sensitive)}) |"
        )

    # worst_case_risk stays a gate and a raw score, but not a row: "how bad is
    # it" and "how likely is the worst case" read as the same question asked
    # twice, which is the duplication the red-team called out on the title rows.
    for dim in ("silent_failure_weighted",):
        value = aggregated.get(dim)
        if value is None:
            continue
        icon, word = answer_label(dim, value)
        lines.append(f"| {QUESTION_TEXT[dim]} | {icon} **{word}** ({as_percent(value)}) |")

    if has_test_changes is not None:
        icon, word = tests_label(has_test_changes, aggregated.get("tests_expected"))
        lines.append(f"| {QUESTION_TEXT['has_tests']} | {icon} **{word}** |")

    cost_text = "less than a cent" if cost_usd < 0.01 else f"${cost_usd:.2f}"
    lines += [
        "",
        "Each percentage is how likely the review thinks that answer is -- not a grade for the code.",
        "Nothing was merged automatically -- this review only leaves a comment.",
        "",
        "<details><summary>Raw scores</summary>",
        "",
        "| dimension | max across files |",
        "|---|---|",
    ]
    for dim in ("risk_level", "worst_case_risk", "diff_matches_title", "hidden_scope",
                "silent_failure", "silent_failure_weighted", "tests_expected",
                "touches_money", "touches_accounts", "touches_personal_data",
                "sensitive_area"):
        value = aggregated.get(dim)
        suffix = " / 3" if dim == "risk_level" else ""
        lines.append(f"| `{dim}` | {value:.2f}{suffix} |" if value is not None else f"| `{dim}` | n/a |")
    lines += [
        "",
        # Moved in from the footer: a cost quoted next to the verdict reads as
        # "this was cheap, therefore it was shallow".
        f"`{total_input_tokens}` input tokens · cost of this run: {cost_text} (${cost_usd:.6f})",
        "",
        "</details>",
        "",
        f"_{files_reviewed} file{'' if files_reviewed == 1 else 's'} reviewed._",
    ]
    return LINE_SEP.join(lines)


# --- Config --------------------------------------------------------------------


def load_config(path: str) -> dict[str, Any]:
    """Load a `.jev-review.yml`/`.jev-review.json` config.

    JSON is parsed with `json.loads`. YAML is parsed with a minimal, deliberately
    narrow parser covering only the flat subset this project uses: top-level
    scalar/mapping keys, one level of nested mapping (`automerge_when:`), and
    one flat list (`blocked_paths:`). Anything beyond that (anchors, multiline
    strings, deep nesting) is out of scope -- use `.jev-review.json` instead,
    as documented in the README.
    """
    if path.endswith(".json"):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return _parse_minimal_yaml(path)


def _parse_minimal_yaml(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        raw_lines = fh.readlines()

    def strip_comment(line: str) -> str:
        # Not fully quote-aware; fine for this project's flat, unquoted config.
        return line.split("#", 1)[0].rstrip()

    def coerce(value: str) -> Any:
        value = value.strip()
        if value == "":
            return None
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            return value[1:-1]
        if value.startswith("'") and value.endswith("'") and len(value) >= 2:
            return value[1:-1]
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        return value

    result: dict[str, Any] = {}
    # Only two indent levels are supported: a top-level key, and its direct
    # children (either a flat list of scalars, or a flat mapping of scalars).
    # That covers this project's actual config shape (`mode`, `automerge_when:`
    # nested scalars, `blocked_paths:` flat list) and nothing deeper.
    pending_top_key: Optional[str] = None
    pending_kind: Optional[str] = None  # "list" | "dict" | None (undecided yet)

    for raw_line in raw_lines:
        line = strip_comment(raw_line)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if indent == 0:
            if stripped.startswith("- "):
                raise ValueError(f"Top-level list item without a parent key: {stripped!r}")
            if ":" not in stripped:
                raise ValueError(f"Unparseable line in minimal YAML: {stripped!r}")
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "":
                pending_top_key = key
                pending_kind = None  # decided by the first child line
                result[key] = None
            else:
                pending_top_key = None
                pending_kind = None
                result[key] = coerce(value)
            continue

        # indent > 0: a child of pending_top_key
        if pending_top_key is None:
            raise ValueError(f"Indented line without a parent key: {stripped!r}")

        if stripped.startswith("- "):
            if pending_kind not in (None, "list"):
                raise ValueError(f"Mixed list/mapping under {pending_top_key!r}: {stripped!r}")
            pending_kind = "list"
            if result[pending_top_key] is None:
                result[pending_top_key] = []
            result[pending_top_key].append(coerce(stripped[2:]))
            continue

        if ":" not in stripped:
            raise ValueError(f"Unparseable line in minimal YAML: {stripped!r}")
        if pending_kind not in (None, "dict"):
            raise ValueError(f"Mixed list/mapping under {pending_top_key!r}: {stripped!r}")
        pending_kind = "dict"
        if result[pending_top_key] is None:
            result[pending_top_key] = {}
        key, _, value = stripped.partition(":")
        result[pending_top_key][key.strip()] = coerce(value.strip())

    return result


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "shadow",
    "automerge_when": {
        "max_risk_level": "< 1.5",
        "hidden_scope": "< 0.25",
        "silent_failure": "< 0.40",
        "diff_matches_title": "> 0.80",
        "max_lines": 400,
    },
    "blocked_paths": [
        ".github/**",
        "**/auth/**",
        "**/migrations/**",
        "**/*secret*",
        "Dockerfile",
    ],
}


# --- Orchestration --------------------------------------------------------------


def review_pr(
    *,
    repo: str,
    pr_number: int,
    github_token: str,
    typesafe_api_key: str,
    config: dict[str, Any],
    dry_run: bool = False,
) -> tuple[str, list[str], dict[str, float], int]:
    """Run the full review and, unless dry_run, upsert the PR comment.

    Returns (verdict, reasons, aggregated_scores, total_input_tokens).
    """
    mode = config.get("mode", "shadow")
    if mode != "shadow":
        raise ValueError(
            f"mode={mode!r} is not supported yet -- only 'shadow' is implemented "
            "until real calibration data exists to safely enable 'enforce'."
        )

    pr = fetch_pr(repo, pr_number, github_token)
    files = fetch_pr_files(repo, pr_number, github_token)
    reviewable = usable_files(files)

    pr_title = pr.get("title", "")
    pr_body = pr.get("body") or ""
    head_sha = (pr.get("head") or {}).get("sha", "")
    ci_status = await_ci_status(
        repo,
        head_sha,
        github_token,
        own_run_id=os.environ.get("GITHUB_RUN_ID", ""),
    )
    has_test_changes = any("test" in f.get("filename", "").lower() for f in files)
    files_changed = [f["filename"] for f in files]
    total_lines_changed = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)

    per_file_answers: list[dict[str, Any]] = []
    total_input_tokens = 0
    network_failure = False

    for f in reviewable:
        state = build_file_state(
            pr_title=pr_title,
            pr_body=pr_body,
            file_path=f["filename"],
            diff=f.get("patch", ""),
            lines_added=f.get("additions", 0),
            lines_deleted=f.get("deletions", 0),
            has_test_changes=has_test_changes,
            ci_status=ci_status,
        )
        try:
            response = call_jev(state, QUESTIONS, api_key=typesafe_api_key)
        except JevClientError:
            network_failure = True
            break
        per_file_answers.append(response.get("answers", {}))
        total_input_tokens += response.get("usage", {}).get("input_tokens", 0)

    aggregated = aggregate_max(per_file_answers)
    verdict, reasons = decide_verdict(
        aggregated=aggregated,
        files_changed=files_changed,
        total_lines_changed=total_lines_changed,
        ci_status=ci_status,
        config=config,
        network_failure=network_failure,
        has_test_changes=has_test_changes,
    )

    if not dry_run:
        comment_body = render_comment(
            verdict=verdict,
            reasons=reasons,
            aggregated=aggregated,
            total_input_tokens=total_input_tokens,
            files_reviewed=len(reviewable),
            pr_title=pr_title,
            pr_author=f"@{(pr.get('user') or {}).get('login', '')}" if (pr.get("user") or {}).get("login") else "",
            files_changed=len(files_changed),
            lines_changed=total_lines_changed,
            has_test_changes=has_test_changes,
            escalate_to=config.get("escalate_to", "") or "",
        )
        upsert_comment(repo, pr_number, github_token, comment_body)

    return verdict, reasons, aggregated, total_input_tokens


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="jev-pr-review: shadow-mode PR review scored by Jev")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument("--config", default=".jev-review.yml", help="Path to config file")
    parser.add_argument("--dry-run", action="store_true", help="Print verdict, do not touch GitHub")
    args = parser.parse_args(argv)

    config = {**DEFAULT_CONFIG, **load_config(args.config)} if os.path.exists(args.config) else DEFAULT_CONFIG

    typesafe_api_key = os.environ.get("TYPESAFE_API_KEY", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not typesafe_api_key:
        print("TYPESAFE_API_KEY is not set", file=sys.stderr)
        return 1
    if not repo:
        print("GITHUB_REPOSITORY is not set", file=sys.stderr)
        return 1
    if not args.dry_run and not github_token:
        print("GITHUB_TOKEN is not set (required unless --dry-run)", file=sys.stderr)
        return 1

    try:
        verdict, reasons, aggregated, total_input_tokens = review_pr(
            repo=repo,
            pr_number=args.pr,
            github_token=github_token,
            typesafe_api_key=typesafe_api_key,
            config=config,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"verdict: {verdict}")
    for reason in reasons:
        print(f"  - {humanize_reason(reason)}  {json.dumps(reason)}")
    print(f"scores: {json.dumps(aggregated, indent=2)}")
    print(f"input tokens: {total_input_tokens}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
