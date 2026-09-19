import pytest

from jev_pr_review import review_pr


def test_enforce_mode_fails_loudly_and_never_reaches_merge_code():
    with pytest.raises(ValueError, match="only 'shadow' is implemented"):
        review_pr(
            repo="owner/repo",
            pr_number=1,
            github_token="fake",
            typesafe_api_key="fake",
            config={"mode": "enforce"},
            dry_run=True,
        )
