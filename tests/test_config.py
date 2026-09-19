import json
import os

from jev_pr_review import load_config


def write(tmp_path, name, content):
    path = os.path.join(str(tmp_path), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def test_load_json_config(tmp_path):
    path = write(
        tmp_path,
        ".jev-review.json",
        json.dumps({"mode": "shadow", "blocked_paths": ["Dockerfile"]}),
    )
    config = load_config(path)
    assert config["mode"] == "shadow"
    assert config["blocked_paths"] == ["Dockerfile"]


def test_load_minimal_yaml_config_matches_example(tmp_path):
    path = write(
        tmp_path,
        ".jev-review.yml",
        """
mode: shadow

automerge_when:
  max_risk_level: "< 1.5"
  hidden_scope: "< 0.25"
  silent_failure: "< 0.40"
  diff_matches_title: "> 0.80"
  max_lines: 400

blocked_paths:
  - ".github/**"
  - "**/auth/**"
  - "**/migrations/**"
  - "**/*secret*"
  - "Dockerfile"
""",
    )
    config = load_config(path)
    assert config["mode"] == "shadow"
    assert config["automerge_when"]["max_risk_level"] == "< 1.5"
    assert config["automerge_when"]["max_lines"] == 400
    assert config["blocked_paths"] == [
        ".github/**",
        "**/auth/**",
        "**/migrations/**",
        "**/*secret*",
        "Dockerfile",
    ]


def test_load_minimal_yaml_ignores_comments_and_blank_lines(tmp_path):
    path = write(
        tmp_path,
        ".jev-review.yml",
        """
# a comment
mode: shadow   # trailing comment

blocked_paths:
  - "Dockerfile"  # another comment
""",
    )
    config = load_config(path)
    assert config["mode"] == "shadow"
    assert config["blocked_paths"] == ["Dockerfile"]
