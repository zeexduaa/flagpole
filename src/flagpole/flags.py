"""Flag definitions, validation and evaluation.

This module is pure: no I/O, no clock, no globals. The server, the CLI and the
client SDK all evaluate flags with the same code, so a flag behaves identically
whether it's checked on the server or inside your app.

A flag has named **variants** (``{"on": true, "off": false}`` or
``{"control": "blue", "treatment": "green"}``). Evaluating it for a **context**
(the user or request: ``{"key": "user-42", "country": "DE", "plan": "pro"}``)
picks one variant:

1. A disabled flag always serves its ``off_variant``.
2. Otherwise the first **rule** whose conditions all match decides.
3. Otherwise the flag's ``default`` decides.

Rules and the default either serve one variant or a percentage **rollout**
across variants. Rollouts hash the context's ``bucket_by`` attribute (``key`` by
default) together with the flag key, so each user always gets the same variant
and different flags split users independently.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ATTRIBUTE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
BUCKETS = 10_000  # rollout precision: 0.01%

LIST_OPERATORS = {"in", "not_in"}
STRING_OPERATORS = {"contains", "not_contains", "starts_with", "ends_with", "matches"}
NUMBER_OPERATORS = {"gt", "gte", "lt", "lte"}
EQUALITY_OPERATORS = {"eq", "neq"}
PRESENCE_OPERATORS = {"exists", "not_exists"}
OPERATORS = LIST_OPERATORS | STRING_OPERATORS | NUMBER_OPERATORS | EQUALITY_OPERATORS | PRESENCE_OPERATORS

MISSING = object()


class FlagError(ValueError):
    """A flag definition is invalid. ``path`` points at the offending field."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}" if path else message)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


# ---- Building blocks -----------------------------------------------------------------------


@dataclass(frozen=True)
class Condition:
    attribute: str
    op: str
    value: Any = None

    @classmethod
    def from_dict(cls, data: Any, path: str) -> Condition:
        if not isinstance(data, Mapping):
            raise FlagError(path, "must be an object like {\"attribute\": \"country\", \"op\": \"eq\", \"value\": \"DE\"}")
        unknown = set(data) - {"attribute", "op", "value"}
        if unknown:
            raise FlagError(path, f"unknown field(s): {', '.join(sorted(unknown))}")
        attribute = data.get("attribute")
        if not isinstance(attribute, str) or not ATTRIBUTE_PATTERN.match(attribute):
            raise FlagError(f"{path}.attribute", "must be a name like \"country\" or \"account.plan\"")
        op = data.get("op")
        if op not in OPERATORS:
            raise FlagError(f"{path}.op", f"unknown operator {op!r}; use one of: {', '.join(sorted(OPERATORS))}")
        value = data.get("value", MISSING)

        if op in PRESENCE_OPERATORS:
            if value is not MISSING:
                raise FlagError(f"{path}.value", f"'{op}' takes no value")
            return cls(attribute, op)
        if value is MISSING:
            raise FlagError(f"{path}.value", f"'{op}' needs a value")
        if op in LIST_OPERATORS:
            if not isinstance(value, list) or not value or not all(_is_scalar(v) for v in value):
                raise FlagError(f"{path}.value", f"'{op}' needs a non-empty list of strings, numbers or booleans")
            return cls(attribute, op, tuple(value))
        if op in STRING_OPERATORS:
            if not isinstance(value, str) or value == "":
                raise FlagError(f"{path}.value", f"'{op}' needs a non-empty string")
            if op == "matches":
                if len(value) > 200:
                    raise FlagError(f"{path}.value", "regular expressions are limited to 200 characters")
                try:
                    re.compile(value)
                except re.error as exc:
                    raise FlagError(f"{path}.value", f"invalid regular expression: {exc}") from None
            return cls(attribute, op, value)
        if op in NUMBER_OPERATORS:
            if not _is_number(value):
                raise FlagError(f"{path}.value", f"'{op}' needs a number")
            return cls(attribute, op, value)
        if not _is_scalar(value):  # eq / neq
            raise FlagError(f"{path}.value", f"'{op}' needs a string, number, boolean or null")
        return cls(attribute, op, value)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"attribute": self.attribute, "op": self.op}
        if self.op not in PRESENCE_OPERATORS:
            out["value"] = list(self.value) if isinstance(self.value, tuple) else self.value
        return out

    def matches(self, context: Mapping[str, Any]) -> bool:
        actual = lookup(context, self.attribute)
        op = self.op
        if op == "exists":
            return actual is not MISSING and actual is not None
        if op == "not_exists":
            return actual is MISSING or actual is None
        if actual is MISSING:
            # Absent attributes match only the negative operators.
            return op in ("neq", "not_in", "not_contains")
        if op == "eq":
            return _same(actual, self.value)
        if op == "neq":
            return not _same(actual, self.value)
        if op == "in":
            return any(_same(actual, v) for v in self.value)
        if op == "not_in":
            return not any(_same(actual, v) for v in self.value)
        if op in NUMBER_OPERATORS:
            if not _is_number(actual):
                return False
            return {
                "gt": actual > self.value,
                "gte": actual >= self.value,
                "lt": actual < self.value,
                "lte": actual <= self.value,
            }[op]
        if not isinstance(actual, str):
            return op == "not_contains"
        if op == "contains":
            return self.value in actual
        if op == "not_contains":
            return self.value not in actual
        if op == "starts_with":
            return actual.startswith(self.value)
        if op == "ends_with":
            return actual.endswith(self.value)
        return _regex(self.value).search(actual) is not None  # matches


_REGEX_CACHE: dict[str, re.Pattern[str]] = {}


def _regex(pattern: str) -> re.Pattern[str]:
    compiled = _REGEX_CACHE.get(pattern)
    if compiled is None:
        if len(_REGEX_CACHE) > 512:
            _REGEX_CACHE.clear()
        compiled = _REGEX_CACHE[pattern] = re.compile(pattern)
    return compiled


def _same(a: Any, b: Any) -> bool:
    """Equality that doesn't treat True as 1 or "1" as 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    return a == b and (_is_number(a) == _is_number(b))


def lookup(context: Mapping[str, Any], attribute: str) -> Any:
    """Reads "country" or a nested "account.plan" from the context; MISSING if absent."""
    current: Any = context
    for part in attribute.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return MISSING
        current = current[part]
    return current


@dataclass(frozen=True)
class Serve:
    """Either one ``variant``, or a ``rollout`` of variant -> percentage (adding up to 100)."""

    variant: str | None = None
    rollout: tuple[tuple[str, float], ...] = ()

    @classmethod
    def from_dict(cls, data: Any, path: str, variants: Mapping[str, Any]) -> Serve:
        if not isinstance(data, Mapping):
            raise FlagError(path, 'must be {"variant": "on"} or {"rollout": {"on": 10, "off": 90}}')
        unknown = set(data) - {"variant", "rollout"}
        if unknown:
            raise FlagError(path, f"unknown field(s): {', '.join(sorted(unknown))}")
        if ("variant" in data) == ("rollout" in data):
            raise FlagError(path, "set exactly one of 'variant' or 'rollout'")
        if "variant" in data:
            variant = data["variant"]
            if variant not in variants:
                raise FlagError(f"{path}.variant", f"unknown variant {variant!r}")
            return cls(variant=variant)

        rollout = data["rollout"]
        if not isinstance(rollout, Mapping) or not rollout:
            raise FlagError(f"{path}.rollout", 'must map variants to percentages, e.g. {"on": 10, "off": 90}')
        weights = []
        for name, pct in rollout.items():
            if name not in variants:
                raise FlagError(f"{path}.rollout", f"unknown variant {name!r}")
            if not _is_number(pct) or pct < 0 or round(pct * 100) != pct * 100:
                raise FlagError(f"{path}.rollout.{name}", "must be a percentage from 0 to 100 with at most 2 decimals")
            weights.append((name, float(pct)))
        total = round(sum(p for _, p in weights) * 100)
        if total != 100 * 100:
            raise FlagError(f"{path}.rollout", f"percentages must add up to 100, not {total / 100:g}")
        return cls(rollout=tuple(weights))

    def to_dict(self) -> dict[str, Any]:
        if self.variant is not None:
            return {"variant": self.variant}
        return {"rollout": {name: (int(p) if p.is_integer() else p) for name, p in self.rollout}}

    def resolve(self, bucket: int | None) -> str | None:
        """The variant to serve. ``bucket`` (0..9999) is needed for rollouts; None means unknown."""
        if self.variant is not None:
            return self.variant
        if bucket is None:
            return None
        upper = 0
        for name, pct in self.rollout:
            upper += round(pct * 100)
            if bucket < upper:
                return name
        return self.rollout[-1][0]  # unreachable when weights add to 100


@dataclass(frozen=True)
class Rule:
    conditions: tuple[Condition, ...]
    serve: Serve
    description: str = ""

    @classmethod
    def from_dict(cls, data: Any, path: str, variants: Mapping[str, Any]) -> Rule:
        if not isinstance(data, Mapping):
            raise FlagError(path, "must be an object with 'conditions' and 'serve'")
        unknown = set(data) - {"conditions", "serve", "description"}
        if unknown:
            raise FlagError(path, f"unknown field(s): {', '.join(sorted(unknown))}")
        raw_conditions = data.get("conditions")
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise FlagError(f"{path}.conditions", "must be a non-empty list (all of them must match)")
        if len(raw_conditions) > 20:
            raise FlagError(f"{path}.conditions", "at most 20 conditions per rule")
        conditions = tuple(Condition.from_dict(c, f"{path}.conditions[{i}]") for i, c in enumerate(raw_conditions))
        description = data.get("description", "")
        if not isinstance(description, str) or len(description) > 200:
            raise FlagError(f"{path}.description", "must be text of at most 200 characters")
        serve = Serve.from_dict(data.get("serve"), f"{path}.serve", variants)
        return cls(conditions, serve, description)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"conditions": [c.to_dict() for c in self.conditions], "serve": self.serve.to_dict()}
        if self.description:
            out["description"] = self.description
        return out

    def matches(self, context: Mapping[str, Any]) -> bool:
        return all(c.matches(context) for c in self.conditions)


# ---- Flags ---------------------------------------------------------------------------------

BOOLEAN_VARIANTS = {"on": True, "off": False}
EDITABLE_FIELDS = {"description", "enabled", "variants", "off_variant", "default", "rules", "bucket_by", "tags"}


@dataclass(frozen=True)
class Flag:
    key: str
    variants: dict[str, Any]
    off_variant: str
    default: Serve
    enabled: bool = False
    rules: tuple[Rule, ...] = ()
    description: str = ""
    bucket_by: str = "key"
    tags: tuple[str, ...] = ()
    #: Set by the store: increases by one on every change.
    version: int = 0
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: Any, *, key: str | None = None) -> Flag:
        """Builds and validates a flag from JSON-like data.

        Omitted fields get friendly defaults: a boolean flag with ``on``/``off``
        variants that is disabled, serves ``off`` when disabled and ``on`` when enabled.
        """
        if not isinstance(data, Mapping):
            raise FlagError("", "a flag must be a JSON object")
        unknown = set(data) - EDITABLE_FIELDS - {"key", "version", "created_at", "updated_at"}
        if unknown:
            raise FlagError("", f"unknown field(s): {', '.join(sorted(unknown))}")

        if key is None:
            key = data.get("key")
        elif "key" in data and data["key"] != key:
            raise FlagError("key", f"doesn't match the flag being saved ({key!r}); flags can't be renamed")
        if not isinstance(key, str) or not KEY_PATTERN.match(key):
            raise FlagError("key", "use 1-64 lowercase letters, digits, '.', '_' or '-', starting with a letter or digit")

        variants = data.get("variants", BOOLEAN_VARIANTS)
        if not isinstance(variants, Mapping) or not 2 <= len(variants) <= 20:
            raise FlagError("variants", "must be an object with 2 to 20 named variants")
        for name in variants:
            if not isinstance(name, str) or not KEY_PATTERN.match(name):
                raise FlagError(f"variants.{name}", "variant names use lowercase letters, digits, '.', '_' or '-'")
        variants = dict(variants)

        off_variant = data.get("off_variant", "off" if "off" in variants else next(iter(variants)))
        if off_variant not in variants:
            raise FlagError("off_variant", f"unknown variant {off_variant!r}")

        if "default" in data:
            default = Serve.from_dict(data["default"], "default", variants)
        else:
            others = [v for v in variants if v != off_variant]
            default = Serve(variant="on" if "on" in variants else others[0])

        raw_rules = data.get("rules", [])
        if not isinstance(raw_rules, list):
            raise FlagError("rules", "must be a list")
        if len(raw_rules) > 100:
            raise FlagError("rules", "at most 100 rules per flag")
        rules = tuple(Rule.from_dict(r, f"rules[{i}]", variants) for i, r in enumerate(raw_rules))

        enabled = data.get("enabled", False)
        if not isinstance(enabled, bool):
            raise FlagError("enabled", "must be true or false")
        description = data.get("description", "")
        if not isinstance(description, str) or len(description) > 500:
            raise FlagError("description", "must be text of at most 500 characters")
        bucket_by = data.get("bucket_by", "key")
        if not isinstance(bucket_by, str) or not ATTRIBUTE_PATTERN.match(bucket_by):
            raise FlagError("bucket_by", "must be a context attribute name like \"key\" or \"account.id\"")
        tags = data.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) and KEY_PATTERN.match(t) for t in tags):
            raise FlagError("tags", "must be a list of short lowercase labels")

        version = data.get("version", 0)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise FlagError("version", "must be a whole number")
        return cls(
            key=key,
            variants=variants,
            off_variant=off_variant,
            default=default,
            enabled=enabled,
            rules=rules,
            description=description,
            bucket_by=bucket_by,
            tags=tuple(dict.fromkeys(tags)),
            version=version,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "enabled": self.enabled,
            "variants": dict(self.variants),
            "off_variant": self.off_variant,
            "default": self.default.to_dict(),
            "rules": [r.to_dict() for r in self.rules],
            "bucket_by": self.bucket_by,
            "tags": list(self.tags),
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def definition(self) -> dict[str, Any]:
        """The editable part only (what a PUT body contains)."""
        data = self.to_dict()
        return {k: v for k, v in data.items() if k in EDITABLE_FIELDS}

    def evaluate(self, context: Mapping[str, Any]) -> Evaluation:
        if not self.enabled:
            return self._result(self.off_variant, "disabled")
        bucket = bucket_for(self.key, lookup(context, self.bucket_by))
        for index, rule in enumerate(self.rules):
            if rule.matches(context):
                return self._serve(rule.serve, bucket, "rule_match", index)
        return self._serve(self.default, bucket, "default", None)

    def _serve(self, serve: Serve, bucket: int | None, reason: str, rule: int | None) -> Evaluation:
        variant = serve.resolve(bucket)
        if variant is None:
            # A rollout needs a bucketing attribute; without one, play safe.
            return self._result(self.off_variant, "missing_bucket_attribute", rule)
        return self._result(variant, reason if serve.variant is not None else f"{reason}_rollout", rule)

    def _result(self, variant: str, reason: str, rule: int | None = None) -> Evaluation:
        return Evaluation(self.key, variant, self.variants[variant], reason, rule, self.version)


@dataclass(frozen=True)
class Evaluation:
    flag: str
    variant: str
    value: Any
    #: Why: disabled, rule_match, rule_match_rollout, default, default_rollout,
    #: missing_bucket_attribute, or (from callers) flag_not_found / error.
    reason: str
    rule: int | None = None
    version: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {"flag": self.flag, "variant": self.variant, "value": self.value, "reason": self.reason}
        if self.rule is not None:
            out["rule"] = self.rule
        out["version"] = self.version
        return out


def bucket_for(flag_key: str, value: Any) -> int | None:
    """A stable bucket 0..9999 for this flag and attribute value; None if there's no usable value."""
    if value is MISSING or value is None or isinstance(value, (dict, list)) or value == "":
        return None
    text = str(value).lower() if isinstance(value, bool) else str(value)
    digest = hashlib.sha256(f"{flag_key}:{text}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS
