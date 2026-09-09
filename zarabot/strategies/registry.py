"""Active strategy set from configuration."""

from __future__ import annotations

from typing import cast

from zarabot.config import Config, ConfigError
from zarabot.strategies.base import Strategy
from zarabot.strategies.ma_crossover import MovingAverageCrossover
from zarabot.strategies.ml_model import load
from zarabot.strategies.momentum import MomentumBreakout
from zarabot.strategies.rsi_reversion import RSIReversion

_RULE_BASED: dict[str, type] = {
    "ma_crossover": MovingAverageCrossover,
    "rsi_reversion": RSIReversion,
    "momentum": MomentumBreakout,
}


def enabled(config: Config) -> list[Strategy]:
    active: list[Strategy] = []
    for name in config.enabled_strategies:
        if name == "ml_model":
            if config.ml_model_path is None:
                continue
            active.append(cast(Strategy, load(config.ml_model_path)))
            continue
        factory = _RULE_BASED.get(name)
        if factory is None:
            raise ConfigError(
                f"ENABLED_STRATEGIES contains unknown name {name}",
                variable="ENABLED_STRATEGIES",
            )
        active.append(factory())
    return active
