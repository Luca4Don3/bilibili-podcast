import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_name_policy.py"
SPEC = importlib.util.spec_from_file_location("check_name_policy", SCRIPT)
POLICY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(POLICY)


def test_fixed_acknowledgement_is_the_only_allowed_content() -> None:
    POLICY.scan_content(
        (POLICY.ACKNOWLEDGEMENT + "\n").encode(),
        "README.md",
        "README.md",
    )

    with pytest.raises(POLICY.PolicyViolation):
        POLICY.scan_content(
            (POLICY.ACKNOWLEDGEMENT + "\n").encode(),
            "docs/credits.md",
            "docs/credits.md",
        )


def test_reserved_name_is_rejected_case_insensitively() -> None:
    with pytest.raises(POLICY.PolicyViolation):
        POLICY.scan_name(POLICY.RESERVED.upper(), "fixture")


def test_current_worktree_satisfies_policy() -> None:
    POLICY.scan_worktree()
