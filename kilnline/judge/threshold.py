"""Named threshold rules.

Every band in the process -- air pressure, zone temperature, entry spacing,
slurry density, roller speed -- is declared once with a name, and then applied
through the same comparison, so an out-of-band value always raises the same
class of error with the same context fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from kilnline.errors import NotFoundError, ThresholdExceeded, ValidationError
from kilnline.temp.curve import WindowVerdict, compare_window


@dataclass(frozen=True)
class ThresholdVerdict:
    """Outcome of applying one rule to one measurement."""

    rule: str
    unit: str
    window: WindowVerdict

    @property
    def value(self) -> float:
        return self.window.value

    @property
    def within(self) -> bool:
        return self.window.within

    @property
    def verdict(self) -> str:
        return self.window.verdict

    @property
    def deviation(self) -> float:
        return self.window.deviation

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "unit": self.unit,
            "value": self.value,
            "low": self.window.low,
            "high": self.window.high,
            "verdict": self.verdict,
            "deviation": self.deviation,
            "within": self.within,
        }


@dataclass(frozen=True)
class ThresholdRule:
    """A named band with an optional unit label."""

    name: str
    low: float
    high: float
    unit: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValidationError("threshold rule needs a name")
        if float(self.low) >= float(self.high):
            raise ValidationError(
                "threshold rule bounds are inverted",
                rule=self.name,
                low=self.low,
                high=self.high,
            )

    def evaluate(self, value: float) -> ThresholdVerdict:
        return ThresholdVerdict(
            rule=self.name,
            unit=self.unit,
            window=compare_window(float(value), low=self.low, high=self.high),
        )

    def require(self, value: float) -> ThresholdVerdict:
        verdict = self.evaluate(value)
        if not verdict.within:
            raise ThresholdExceeded(
                "value left its allowed band",
                rule=self.name,
                value=verdict.value,
                low=self.low,
                high=self.high,
                unit=self.unit,
                verdict=verdict.verdict,
                deviation=verdict.deviation,
            )
        return verdict

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "low": self.low,
            "high": self.high,
            "unit": self.unit,
            "description": self.description,
        }


class RuleBook:
    """Registry of threshold rules, addressed by name."""

    def __init__(self) -> None:
        self._rules: dict[str, ThresholdRule] = {}

    def declare(
        self,
        name: str,
        *,
        low: float,
        high: float,
        unit: str = "",
        description: str = "",
    ) -> ThresholdRule:
        rule = ThresholdRule(name=str(name), low=float(low), high=float(high), unit=str(unit), description=str(description))
        self._rules[rule.name] = rule
        return rule

    def rule(self, name: str) -> ThresholdRule:
        label = str(name).strip()
        rule = self._rules.get(label)
        if rule is None:
            raise NotFoundError("unknown threshold rule", rule=label)
        return rule

    def evaluate(self, name: str, value: float) -> ThresholdVerdict:
        return self.rule(name).evaluate(value)

    def require(self, name: str, value: float) -> ThresholdVerdict:
        return self.rule(name).require(value)

    def evaluate_all(self, values: Mapping[str, float]) -> list[ThresholdVerdict]:
        return [self.rule(name).evaluate(value) for name, value in sorted(values.items())]

    def violations(self, values: Mapping[str, float]) -> list[ThresholdVerdict]:
        return [verdict for verdict in self.evaluate_all(values) if not verdict.within]

    def names(self) -> list[str]:
        return sorted(self._rules)

    def snapshot(self) -> dict[str, Any]:
        return {name: rule.as_dict() for name, rule in sorted(self._rules.items())}
