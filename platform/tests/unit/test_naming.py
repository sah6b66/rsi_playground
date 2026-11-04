"""Tests for rsi.core.naming (DD-01 §4.1)."""

import re

import pytest

from rsi.core.errors import NamingError
from rsi.core.naming import (
    PREFIXES,
    git_refs,
    new_id,
    validate_evalset_id,
    validate_id,
    validate_sut_id,
    validate_task_id,
)

CROCKFORD_LOWER = set("0123456789abcdefghjkmnpqrstvwxyz")  # excludes i, l, o, u


class TestNewId:
    def test_format(self):
        eid = new_id("run")
        parts = eid.split("-")
        assert len(parts) == 3
        assert parts[0] == "run"
        assert len(parts[1]) == 14  # YYYYMMDDHHmmss
        assert len(parts[2]) == 8  # random

    def test_timestamp_is_digits(self):
        ts = new_id("batch").split("-")[1]
        assert ts.isdigit()

    def test_random_is_crockford_base32(self):
        rand = new_id("lab").split("-")[2]
        assert set(rand) <= CROCKFORD_LOWER

    def test_unique(self):
        ids = {new_id("run") for _ in range(100)}
        assert len(ids) == 100

    @pytest.mark.parametrize("prefix", sorted(PREFIXES))
    def test_all_prefixes_accepted(self, prefix):
        assert new_id(prefix).startswith(prefix + "-")

    def test_invalid_prefix(self):
        with pytest.raises(NamingError):
            new_id("bogus")


class TestPrefixes:
    def test_frozenset_members(self):
        assert PREFIXES == frozenset(
            {"batch", "run", "lab", "snap", "iter", "jd", "hyp", "exp", "inst", "cred"}
        )


class TestValidateId:
    def test_valid(self):
        validate_id("run", new_id("run"))  # no exception

    def test_wrong_prefix(self):
        with pytest.raises(NamingError):
            validate_id("run", new_id("batch"))

    def test_malformed(self):
        for bad in ["run", "run-20260101120000", "run-20260101120000-abc", "", "run-x-y-z"]:
            with pytest.raises(NamingError):
                validate_id("run", bad)

    def test_bad_timestamp(self):
        with pytest.raises(NamingError):
            validate_id("run", "run-20261301120000-abcdefgh")

    def test_bad_random_charset(self):
        with pytest.raises(NamingError):
            validate_id("run", "run-20260101120000-abcdijkl")  # i/l illegal


class TestValidateSutId:
    def test_valid(self):
        validate_sut_id("ipops-agent")  # no exception

    def test_valid_min_length(self):
        validate_sut_id("abc")

    def test_valid_32_chars(self):
        validate_sut_id("a" + "b" * 31)

    def test_too_short(self):
        with pytest.raises(NamingError):
            validate_sut_id("ab")  # min 3 chars

    def test_too_long(self):
        with pytest.raises(NamingError):
            validate_sut_id("a" * 33)

    def test_uppercase_rejected(self):
        with pytest.raises(NamingError):
            validate_sut_id("MyAgent")

    def test_leading_digit_rejected(self):
        with pytest.raises(NamingError):
            validate_sut_id("1abc")

    def test_underscore_rejected(self):
        with pytest.raises(NamingError):
            validate_sut_id("abc_def")


class TestValidateEvalsetId:
    @pytest.mark.parametrize(
        "v", ["bgp-holdout", "routing-iterate", "ospf-smoke", "telemetry-general"]
    )
    def test_valid(self, v):
        validate_evalset_id(v)

    @pytest.mark.parametrize(
        "v", ["bgp", "bgp-train", "bgp-holdout-x", "-holdout", "bgp-", "BGP-holdout", "a-b-c"]
    )
    def test_invalid(self, v):
        with pytest.raises(NamingError):
            validate_evalset_id(v)


class TestValidateTaskId:
    def test_valid(self):
        validate_task_id("fault/bgp-peer-down-01")

    def test_valid_other_categories(self):
        validate_task_id("inspection/lfdb-stale-12")
        validate_task_id("cutover/isis-restart-99")

    def test_invalid_category(self):
        with pytest.raises(NamingError):
            validate_task_id("bogus/bgp-peer-down-01")

    def test_bad_seq(self):
        with pytest.raises(NamingError):
            validate_task_id("fault/bgp-peer-down-1")
        with pytest.raises(NamingError):
            validate_task_id("fault/bgp-peer-down-1a")

    def test_no_slash(self):
        with pytest.raises(NamingError):
            validate_task_id("bgp-peer-down-01")


class TestGitRefs:
    def test_tuple(self):
        baseline, cand = git_refs("ipops-agent", 3)
        assert baseline == "suts/ipops-agent/baseline/v3"
        assert cand.startswith("suts/ipops-agent/cand/")

    def test_cand_with_hyp(self):
        _, cand = git_refs("ipops-agent", 1, hyp_id="hyp-20260101120000-abcdefgh")
        assert cand == "suts/ipops-agent/cand/hyp-20260101120000-abcdefgh"

    def test_invalid_sut(self):
        with pytest.raises(NamingError):
            git_refs("AB", 1)

    def test_invalid_n(self):
        with pytest.raises(NamingError):
            git_refs("ipops-agent", 0)
