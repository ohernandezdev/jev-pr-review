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
        derived = derive_dimensions(per_file)
        for dim, value in derived.items():
            if dim not in result or value > result[dim]:
                result[dim] = value
    return result


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
        reasons.append(
            "blocked path(s) touched: " + ", ".join(matched_blocked)
        )

    if total_lines_changed > max_lines:
        reasons.append(
            f"changed lines ({total_lines_changed}) exceed max_lines ({max_lines})"
        )

    if ci_status.strip().lower() not in {"all checks passing", "success", "passing"}:
        reasons.append(f"CI is not green (ci_status={ci_status!r})")

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
) -> tuple[str, list[str]]:
    """Compute the final verdict and its reasons.

    Fail-safe: any network failure after retries forces `escalate`, never
    `automerge`, regardless of everything else.
    """
    if network_failure:
        return "escalate", ["Jev API unreachable after retries -- fail-safe escalate"]

    gate_reasons = evaluate_hard_gates(
        files_changed=files_changed,
        total_lines_changed=total_lines_changed,
        ci_status=ci_status,
        blocked_paths=config.get("blocked_paths", []),
        max_lines=config.get("automerge_when", {}).get("max_lines", 400),
    )
    if gate_reasons:
        return "escalate", gate_reasons

    thresholds = config.get("automerge_when", {})
    reasons: list[str] = []
    dimension_thresholds = {
        "risk_level": thresholds.get("max_risk_level"),
        "hidden_scope": thresholds.get("hidden_scope"),
        "silent_failure_weighted": thresholds.get("silent_failure_weighted"),
        "diff_matches_title": thresholds.get("diff_matches_title"),
    }
    for dim, expr in dimension_thresholds.items():
        if expr is None:
            continue
        value = aggregated.get(dim)
        if value is None:
            reasons.append(f"no score available for {dim}")
            continue
        if not check_threshold(value, expr):
            reasons.append(f"{dim}={value:.2f} fails threshold '{expr}'")

    if reasons:
        return "escalate", reasons
    return "automerge", ["all dimensions within configured thresholds, no gate triggered"]


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


def fetch_check_runs(repo: str, sha: str, token: Optional[str]) -> list[dict[str, Any]]:
    """Check runs for a commit. An unreadable list is treated as no checks."""
    try:
        payload = _github_request(f"/repos/{repo}/commits/{sha}/check-runs", token)
    except Exception:
        return []
    return payload.get("check_runs", []) or []


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
    deadline = now_fn() + timeout_s
    status = compute_ci_status(fetch_check_runs(repo, sha, token), own_run_id=own_run_id)
    while status in {"pending", "no checks"} and now_fn() < deadline:
        sleep_fn(poll_s)
        status = compute_ci_status(fetch_check_runs(repo, sha, token), own_run_id=own_run_id)
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


def render_comment(
    *,
    verdict: str,
    reasons: list[str],
    aggregated: dict[str, float],
    total_input_tokens: int,
) -> str:
    cost_usd = total_input_tokens * COST_PER_MILLION_INPUT_TOKENS / 1_000_000
    lines = [
        "## jev-pr-review (shadow mode)",
        "",
        f"**Verdict: `{verdict}`** (shadow mode -- informational only, nothing is merged automatically)",
        "",
        "| Dimension | Max score across files |",
        "|---|---|",
    ]
    for dim in ("risk_level", "diff_matches_title", "hidden_scope", "silent_failure", "silent_failure_weighted", "tests_expected"):
        value = aggregated.get(dim)
        lines.append(f"| `{dim}` | {value:.2f} |" if value is not None else f"| `{dim}` | n/a |")

    lines += [
        "",
        "**Reasons:**",
    ]
    lines += [f"- {reason}" for reason in reasons] or ["- (none)"]

    lines += [
        "",
        f"_Cost of this run: ${cost_usd:.6f} ({total_input_tokens} input tokens)._",
    ]
    return "\n".join(lines)


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
    )

    if not dry_run:
        comment_body = render_comment(
            verdict=verdict,
            reasons=reasons,
            aggregated=aggregated,
            total_input_tokens=total_input_tokens,
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
        print(f"  - {reason}")
    print(f"scores: {json.dumps(aggregated, indent=2)}")
    print(f"input tokens: {total_input_tokens}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
