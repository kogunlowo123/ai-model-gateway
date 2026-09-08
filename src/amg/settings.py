"""Configuration, from the environment, with the dangerous defaults refused.

Everything here is optional and everything has a default that is safe to run
with. There is **no credential of any kind**: this gateway routes between
simulated upstreams by default, and the one real adapter it ships talks to a
local model server that authenticates to nothing. If you find yourself wanting
to add an API key here, you are configuring a provider adapter that does not
exist yet -- see ``docs/upstreams.md``.

Two settings refuse rather than degrade.

**A fitted policy with no estimator raises at construction.** Silently falling
back to always-cheapest would produce a gateway that is healthy, cheap, and
answering worse than it should, with no symptom except a quality number nobody
is watching.

**A deadline below the per-attempt timeout raises.** It can never allow one
complete attempt, so every request would expire, and the failure would look like
an upstream problem.

**A misspelt variable is caught explicitly, because `extra="forbid"` does not
catch it.** Measured: with ``env_prefix="AMG_"`` set, ``AMG_CONCURENCY=48``
constructs cleanly and the concurrency stays at its default. pydantic-settings
matches known field names against the environment rather than the reverse, so an
unmatched prefixed variable is never seen and there is nothing for ``forbid`` to
reject. That is the worst possible failure mode for configuration -- a setting
somebody deliberately chose, silently doing nothing -- so
:func:`unknown_variables` scans the other direction and
:meth:`Settings.from_environment` refuses.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from amg.errors import ConfigError
from amg.gateway import GatewayConfig
from amg.resilience.retry import (
    DEFAULT_BACKOFF_BASE_US,
    DEFAULT_CONCURRENCY,
    DEFAULT_DEADLINE_US,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_US,
    RetryPolicy,
)
from amg.routing.calibrate import DEFAULT_BUDGET_MULTIPLE
from amg.routing.estimator import Estimator
from amg.routing.policies import POLICY_NAMES, Policy, build


class Settings(BaseSettings):
    """Everything the gateway reads from the environment.

    Prefixed ``AMG_`` so the variables cannot collide with anything else on a
    shared host, and ``extra="forbid"`` so a misspelt variable fails at startup
    rather than being silently ignored -- the failure mode where a carefully set
    ``AMG_CONCURENCY`` does nothing for six months.
    """

    model_config = SettingsConfigDict(
        env_prefix="AMG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    policy: Literal["cheapest", "best", "cascade", "blend", "fitted"] = "cascade"
    estimator_path: Path | None = None
    threshold_high: int | None = None
    threshold_low: int | None = None
    blend_to_cheap: int | None = None
    blend_to_middle: int | None = None
    budget_multiple: int = Field(default=DEFAULT_BUDGET_MULTIPLE, ge=1)

    max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=10)
    backoff_base_us: int = Field(default=DEFAULT_BACKOFF_BASE_US, ge=0)
    timeout_us: int = Field(default=DEFAULT_TIMEOUT_US, ge=1)
    deadline_us: int = Field(default=DEFAULT_DEADLINE_US, ge=1)
    concurrency: int = Field(default=DEFAULT_CONCURRENCY, ge=1)
    breaker_enabled: bool = True

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.deadline_us < self.timeout_us:
            raise ValueError(
                f"AMG_DEADLINE_US ({self.deadline_us}) is below AMG_TIMEOUT_US "
                f"({self.timeout_us}); the deadline covers queueing plus the call, "
                "so this can never allow one complete attempt"
            )
        if self.policy == "fitted" and self.estimator_path is None:
            raise ValueError(
                "AMG_POLICY=fitted needs AMG_ESTIMATOR_PATH. Falling back to another "
                "policy would hide the misconfiguration behind a healthy-looking "
                "gateway that quietly answers worse"
            )
        if self.policy == "blend" and (self.blend_to_cheap is None or self.blend_to_middle is None):
            raise ValueError(
                "AMG_POLICY=blend needs AMG_BLEND_TO_CHEAP and AMG_BLEND_TO_MIDDLE; "
                "run `amg calibrate` to size them against the fitted policy's spend"
            )
        return self

    @classmethod
    def from_environment(cls) -> Settings:
        """Build from the environment, refusing a variable nothing reads.

        The entry point every executable should use. Constructing ``Settings()``
        directly is fine in a test that passes its fields explicitly; it is the
        wrong thing at a process boundary, where a typo needs to be loud.
        """
        unknown = unknown_variables()
        if unknown:
            raise ConfigError(
                f"these environment variables are set and nothing reads them: {', '.join(unknown)}",
                remedy=(
                    "Check the spelling against: " + ", ".join(known_variables()) + ". "
                    "A setting somebody chose that silently does nothing is worse "
                    "than one that fails at startup."
                ),
            )
        return cls()

    def gateway(self) -> GatewayConfig:
        """The runtime configuration this environment describes."""
        return GatewayConfig(
            retry=RetryPolicy(
                max_attempts=self.max_attempts,
                backoff_base_us=self.backoff_base_us,
                timeout_us=self.timeout_us,
            ),
            concurrency=self.concurrency,
            deadline_us=self.deadline_us,
            breaker_enabled=self.breaker_enabled,
        )

    def build_policy(self) -> Policy:
        """Construct the configured policy, loading an estimator if one is needed."""
        estimator = Estimator.load(self.estimator_path) if self.estimator_path else None
        thresholds = (
            (self.threshold_high, self.threshold_low)
            if self.threshold_high is not None and self.threshold_low is not None
            else None
        )
        shares = (
            (self.blend_to_cheap, self.blend_to_middle)
            if self.blend_to_cheap is not None and self.blend_to_middle is not None
            else None
        )
        if self.policy == "fitted" and thresholds is None:
            raise ConfigError(
                "AMG_POLICY=fitted needs AMG_THRESHOLD_HIGH and AMG_THRESHOLD_LOW",
                remedy="Run `amg calibrate` and copy the two numbers it prints.",
            )
        return build(self.policy, estimator=estimator, thresholds=thresholds, shares=shares)


def known_variables() -> tuple[str, ...]:
    """Every environment variable this package reads."""
    return tuple(sorted(f"AMG_{name.upper()}" for name in Settings.model_fields))


def unknown_variables(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Prefixed variables that are set and that nothing reads.

    Scans the environment for ``AMG_*`` and subtracts the known names -- the
    opposite direction from the one pydantic-settings walks, and the only one
    that can see a typo. See this module's docstring for why ``extra="forbid"``
    is not enough.
    """
    known = set(known_variables())
    source = os.environ if environ is None else environ
    return tuple(sorted(name for name in source if name.startswith("AMG_") and name not in known))


__all__ = [
    "POLICY_NAMES",
    "Settings",
    "known_variables",
    "unknown_variables",
]
