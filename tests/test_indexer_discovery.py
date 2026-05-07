from __future__ import annotations

from collections import Counter

from gpc.indexer import (
    IndexOptions,
    _candidate_from_path,
    _looks_sensitive_filename,
    _matches_gitignore,
)


def test_candidate_from_path_skips_sensitive_files_and_allows_safe_env_examples(
    tmp_path,
) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=abc", encoding="utf-8")
    sample = tmp_path / ".env.example"
    sample.write_text("TOKEN=example", encoding="utf-8")
    skipped: Counter[str] = Counter()

    assert _candidate_from_path(tmp_path, secret, IndexOptions(), skipped) is None
    candidate = _candidate_from_path(tmp_path, sample, IndexOptions(), skipped)

    assert skipped["sensitive_filename"] == 1
    assert candidate is not None
    assert candidate.relative_path == ".env.example"


def test_candidate_from_path_respects_unknown_text_policy(tmp_path) -> None:
    unknown = tmp_path / "notes.custom"
    unknown.write_text("plain text", encoding="utf-8")

    skipped: Counter[str] = Counter()
    assert _candidate_from_path(tmp_path, unknown, IndexOptions(), skipped) is None
    assert skipped["unsupported_extension"] == 1

    allowed = _candidate_from_path(
        tmp_path,
        unknown,
        IndexOptions(include_unknown_text=True),
        Counter(),
    )
    assert allowed is not None
    assert allowed.relative_path == "notes.custom"


def test_gitignore_matching_handles_anchored_directory_and_basename_patterns() -> None:
    assert _matches_gitignore("build", is_dir=True, patterns=["build/"])
    assert not _matches_gitignore("src/build.py", is_dir=False, patterns=["build/"])
    assert _matches_gitignore(
        "src/generated/output.py", is_dir=False, patterns=["/src/generated/*"]
    )
    assert _matches_gitignore("docs/tmp/cache.txt", is_dir=False, patterns=["tmp"])


def test_sensitive_filename_policy_keeps_examples_but_blocks_private_keys() -> None:
    assert _looks_sensitive_filename(".env.example") is False
    assert _looks_sensitive_filename("id_rsa") is True
    assert _looks_sensitive_filename("service.secret") is True
