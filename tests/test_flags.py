from collections import Counter

import pytest

from flagpole.flags import Condition, Flag, FlagError, bucket_for, lookup, MISSING


def flag(**overrides):
    data = {"key": "new-checkout", "enabled": True}
    data.update(overrides)
    return Flag.from_dict(data)


# ---- Defaults and serialisation ------------------------------------------------------------


def test_minimal_flag_is_a_disabled_boolean():
    f = Flag.from_dict({"key": "dark-mode"})
    assert f.variants == {"on": True, "off": False}
    assert f.enabled is False
    assert f.off_variant == "off"
    assert f.default.variant == "on"
    assert f.evaluate({"key": "u1"}).to_dict() == {
        "flag": "dark-mode", "variant": "off", "value": False, "reason": "disabled", "version": 0,
    }


def test_round_trip_through_dict():
    data = {
        "key": "pricing-page",
        "description": "New pricing layout",
        "enabled": True,
        "variants": {"control": "v1", "treatment": {"layout": "v2", "banner": True}},
        "off_variant": "control",
        "default": {"rollout": {"control": 50, "treatment": 50}},
        "rules": [
            {
                "description": "Staff always see it",
                "conditions": [{"attribute": "email", "op": "ends_with", "value": "@example.com"}],
                "serve": {"variant": "treatment"},
            }
        ],
        "bucket_by": "account.id",
        "tags": ["growth", "q4"],
    }
    f = Flag.from_dict(data)
    again = Flag.from_dict(f.to_dict())
    assert again == f
    assert f.definition() == {k: v for k, v in data.items() if k != "key"}  # editable fields only


def test_multivariate_defaults_pick_sensible_variants():
    f = Flag.from_dict({"key": "theme", "variants": {"blue": 1, "green": 2, "red": 3}})
    assert f.off_variant == "blue"
    assert f.default.variant == "green"


# ---- Evaluation ----------------------------------------------------------------------------


def test_rules_are_checked_in_order_and_all_conditions_must_match():
    f = flag(
        rules=[
            {"conditions": [{"attribute": "country", "op": "eq", "value": "DE"},
                            {"attribute": "plan", "op": "eq", "value": "pro"}],
             "serve": {"variant": "on"}},
            {"conditions": [{"attribute": "country", "op": "eq", "value": "DE"}], "serve": {"variant": "off"}},
        ],
        default={"variant": "off"},
    )
    assert f.evaluate({"key": "a", "country": "DE", "plan": "pro"}).to_dict()["rule"] == 0
    e = f.evaluate({"key": "a", "country": "DE", "plan": "free"})
    assert (e.variant, e.reason, e.rule) == ("off", "rule_match", 1)
    e = f.evaluate({"key": "a", "country": "FR"})
    assert (e.variant, e.reason, e.rule) == ("off", "default", None)


def test_disabled_flag_ignores_rules():
    f = flag(enabled=False, rules=[{"conditions": [{"attribute": "key", "op": "exists"}], "serve": {"variant": "on"}}])
    assert f.evaluate({"key": "a"}).reason == "disabled"


def test_rollout_is_stable_and_close_to_the_requested_split():
    f = flag(default={"rollout": {"on": 20, "off": 80}})
    results = [f.evaluate({"key": f"user-{i}"}) for i in range(20_000)]
    counts = Counter(r.variant for r in results)
    assert abs(counts["on"] / 20_000 - 0.20) < 0.01
    assert {r.reason for r in results} == {"default_rollout"}
    # Same user, same answer, every time.
    assert all(f.evaluate({"key": "user-7"}).variant == results[7].variant for _ in range(5))


def test_rollout_increase_keeps_existing_users_in():
    small = flag(default={"rollout": {"on": 10, "off": 90}})
    large = flag(default={"rollout": {"on": 30, "off": 70}})
    users = [{"key": f"u{i}"} for i in range(5000)]
    on_small = {u["key"] for u in users if small.evaluate(u).variant == "on"}
    on_large = {u["key"] for u in users if large.evaluate(u).variant == "on"}
    assert on_small <= on_large  # growing a rollout never takes it away from anyone


def test_different_flags_split_users_independently():
    a = flag(key="flag-a", default={"rollout": {"on": 50, "off": 50}})
    b = flag(key="flag-b", default={"rollout": {"on": 50, "off": 50}})
    users = [{"key": f"u{i}"} for i in range(4000)]
    same = sum(a.evaluate(u).variant == b.evaluate(u).variant for u in users)
    assert abs(same / 4000 - 0.5) < 0.05  # unrelated, not identical


def test_rollout_without_bucket_attribute_serves_off_variant():
    f = flag(default={"rollout": {"on": 50, "off": 50}}, bucket_by="account.id")
    e = f.evaluate({"key": "u1"})
    assert (e.variant, e.reason) == ("off", "missing_bucket_attribute")
    assert f.evaluate({"account": {"id": 99}}).reason == "default_rollout"


def test_zero_and_hundred_percent():
    assert all(flag(default={"rollout": {"on": 0, "off": 100}}).evaluate({"key": f"u{i}"}).variant == "off"
               for i in range(500))
    assert all(flag(default={"rollout": {"on": 100, "off": 0}}).evaluate({"key": f"u{i}"}).variant == "on"
               for i in range(500))


def test_fractional_percentages():
    f = flag(default={"rollout": {"on": 0.5, "off": 99.5}})
    on = sum(f.evaluate({"key": f"u{i}"}).variant == "on" for i in range(40_000))
    assert 120 < on < 280  # about 200


def test_bucket_for():
    assert bucket_for("f", "user-1") == bucket_for("f", "user-1")
    assert 0 <= bucket_for("f", "user-1") < 10_000
    assert bucket_for("f", 42) == bucket_for("f", "42")
    assert bucket_for("f", True) == bucket_for("f", "true")
    for missing in (MISSING, None, "", {}, []):
        assert bucket_for("f", missing) is None


# ---- Operators -----------------------------------------------------------------------------


def cond(op, value=MISSING, attribute="x"):
    data = {"attribute": attribute, "op": op}
    if value is not MISSING:
        data["value"] = value
    return Condition.from_dict(data, "c")


@pytest.mark.parametrize(
    "op, value, actual, expected",
    [
        ("eq", "DE", "DE", True),
        ("eq", "DE", "de", False),
        ("eq", 1, True, False),  # True is not 1
        ("eq", 1, "1", False),  # "1" is not 1
        ("eq", 1, 1.0, True),
        ("eq", None, None, True),
        ("neq", "DE", "FR", True),
        ("in", ["DE", "AT"], "AT", True),
        ("in", ["DE", "AT"], "CH", False),
        ("not_in", ["DE"], "FR", True),
        ("contains", "beta", "beta-tester", True),
        ("not_contains", "beta", "regular", True),
        ("starts_with", "ad", "admin", True),
        ("ends_with", "@example.com", "sam@example.com", True),
        ("ends_with", "@example.com", 5, False),
        ("matches", r"^\d{3}$", "123", True),
        ("matches", r"^\d{3}$", "1234", False),
        ("gt", 10, 11, True),
        ("gt", 10, 10, False),
        ("gte", 10, 10, True),
        ("lt", 1.5, 1.4, True),
        ("lte", 2, "1", False),  # strings never compare as numbers
        ("lt", 5, True, False),
    ],
)
def test_operators(op, value, actual, expected):
    assert cond(op, value).matches({"x": actual}) is expected


@pytest.mark.parametrize(
    "op, value, expected",
    [("eq", "a", False), ("neq", "a", True), ("in", ["a"], False), ("not_in", ["a"], True),
     ("contains", "a", False), ("not_contains", "a", True), ("gt", 1, False)],
)
def test_missing_attributes_match_only_negative_operators(op, value, expected):
    assert cond(op, value).matches({}) is expected


def test_presence_operators():
    assert cond("exists").matches({"x": 0})
    assert not cond("exists").matches({"x": None})
    assert cond("not_exists").matches({})


def test_nested_attributes():
    c = cond("eq", "pro", attribute="account.plan")
    assert c.matches({"account": {"plan": "pro"}})
    assert not c.matches({"account": "pro"})
    assert lookup({"a": {"b": {"c": 1}}}, "a.b.c") == 1
    assert lookup({"a": 1}, "a.b") is MISSING


# ---- Validation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data, path, message",
    [
        ({"key": "Bad Key"}, "key", "lowercase"),
        ({"key": "x", "variants": {"only": 1}}, "variants", "2 to 20"),
        ({"key": "x", "variants": {"On": 1, "off": 0}}, "variants.On", "lowercase"),
        ({"key": "x", "off_variant": "nope"}, "off_variant", "unknown variant"),
        ({"key": "x", "default": {"variant": "maybe"}}, "default.variant", "unknown variant"),
        ({"key": "x", "default": {"rollout": {"on": 60, "off": 30}}}, "default.rollout", "add up to 100, not 90"),
        ({"key": "x", "default": {"rollout": {"on": 50.123, "off": 49.877}}}, "default.rollout.on", "2 decimals"),
        ({"key": "x", "default": {"variant": "on", "rollout": {"on": 100}}}, "default", "exactly one"),
        ({"key": "x", "rules": [{"conditions": [], "serve": {"variant": "on"}}]}, "rules[0].conditions", "non-empty"),
        ({"key": "x", "rules": [{"conditions": [{"attribute": "a", "op": "equals", "value": 1}],
                                 "serve": {"variant": "on"}}]}, "rules[0].conditions[0].op", "unknown operator"),
        ({"key": "x", "rules": [{"conditions": [{"attribute": "a", "op": "in", "value": "DE"}],
                                 "serve": {"variant": "on"}}]}, "rules[0].conditions[0].value", "list"),
        ({"key": "x", "rules": [{"conditions": [{"attribute": "a", "op": "matches", "value": "("}],
                                 "serve": {"variant": "on"}}]}, "rules[0].conditions[0].value", "regular expression"),
        ({"key": "x", "rules": [{"conditions": [{"attribute": "a", "op": "exists", "value": 1}],
                                 "serve": {"variant": "on"}}]}, "rules[0].conditions[0].value", "takes no value"),
        ({"key": "x", "rules": [{"conditions": [{"attribute": "1bad", "op": "exists"}],
                                 "serve": {"variant": "on"}}]}, "rules[0].conditions[0].attribute", "name like"),
        ({"key": "x", "enabled": "yes"}, "enabled", "true or false"),
        ({"key": "x", "owner": "me"}, "", "unknown field"),
        ({"key": "x", "tags": ["Has Space"]}, "tags", "labels"),
    ],
)
def test_validation_points_at_the_problem(data, path, message):
    with pytest.raises(FlagError) as info:
        Flag.from_dict(data)
    assert info.value.path == path
    assert message in info.value.message


def test_key_cannot_change_on_save():
    with pytest.raises(FlagError, match="can't be renamed"):
        Flag.from_dict({"key": "other"}, key="dark-mode")
    assert Flag.from_dict({"enabled": True}, key="dark-mode").key == "dark-mode"


def test_evaluation_is_fast_enough():
    f = flag(
        rules=[{"conditions": [{"attribute": "country", "op": "in", "value": [f"C{i}" for i in range(50)]}],
                "serve": {"variant": "on"}} for _ in range(20)],
        default={"rollout": {"on": 25, "off": 75}},
    )
    import time

    start = time.perf_counter()
    for i in range(5000):
        f.evaluate({"key": f"u{i}", "country": "XX"})
    per_eval_us = (time.perf_counter() - start) / 5000 * 1e6
    assert per_eval_us < 500  # 20 rules x 50-item lists; generous so slow CI runners stay green
