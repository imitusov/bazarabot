"""Tests for scripts/research/train.py — the training entrypoint (#212).

`sandbox.train.fit`/`export` were reachable only from this suite. These pin what
a runner has to preserve: `seed` is required rather than defaulted (an
unreproducible model cannot be audited), the per-fold scores are reported rather
than averaged, the candles come from `sandbox.data.load`, and the bundle lands
under the gitignored `sandbox/models/` so a model file cannot be committed.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from sandbox.train import FittedModel
from zarabot.models import Candle
from zarabot.strategies.ml_model import FEATURE_NAMES

DAY0 = datetime(2026, 3, 16, 7, 0, tzinfo=UTC)
NOW = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)


def _mod() -> ModuleType:
    path = Path("scripts/research/train.py")
    spec = importlib.util.spec_from_file_location("research_train", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candles(count: int) -> list[Candle]:
    out: list[Candle] = []
    for n in range(count):
        price = Decimal("100") + Decimal(n)
        out.append(
            Candle(
                timestamp=DAY0 + timedelta(days=n),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1000,
            )
        )
    return out


def _model() -> FittedModel:
    return FittedModel(
        estimator=object(),
        features=FEATURE_NAMES,
        seed=7,
        trained_at=NOW,
        fold_scores=(Decimal("0.61"), Decimal("0.48"), Decimal("0.73")),
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The script with every seam replaced: no network, no clock, no env."""
    mod = _mod()
    # Built from a dict so the token-shaped fields are values, not keyword
    # arguments: ruff's S106 flags the latter, and the point of these two is
    # that the runner never prints them.
    cfg = SimpleNamespace(
        **{
            "watchlist": ("SBER", "GAZP"),
            "tinvest_token": "SECRET-TOKEN-VALUE",
            "telegram_bot_token": "SECRET-TELEGRAM-TOKEN",
        }
    )
    model = _model()
    calls: dict[str, Any] = {"load": [], "fit": None, "export": None}

    async def _load(
        ticker: str,
        start: datetime,
        end: datetime,
        interval: object,
        cache_dir: Path = Path("sandbox/cache"),
    ) -> list[Candle]:
        calls["load"].append((ticker, start, end, interval, cache_dir))
        return _candles(20)

    def _fit(**kwargs: object) -> FittedModel:
        calls["fit"] = kwargs
        return model

    def _export(fitted: FittedModel, path: Path) -> Path:
        calls["export"] = (fitted, path)
        return path

    monkeypatch.setattr(mod, "load", _load)
    monkeypatch.setattr(mod, "fit", _fit)
    monkeypatch.setattr(mod, "export", _export)
    monkeypatch.setattr(mod, "config", SimpleNamespace(get=lambda: cfg))
    return SimpleNamespace(mod=mod, cfg=cfg, model=model, calls=calls)


def test_seed_is_required_rather_than_defaulted() -> None:
    """An unreproducible model cannot be audited after a losing week."""
    mod = _mod()
    with pytest.raises(SystemExit):
        mod.parse_args(["--start", "2025-01-01", "--end", "2026-01-01"])


def test_dates_reach_sandbox_data_timezone_aware() -> None:
    mod = _mod()
    args = mod.parse_args(
        ["--start", "2025-01-01", "--end", "2026-01-01", "--seed", "7"]
    )
    assert args.start == datetime(2025, 1, 1, tzinfo=UTC)
    assert args.end == datetime(2026, 1, 1, tzinfo=UTC)


def test_the_default_bundle_path_is_gitignored() -> None:
    """Rule: never commit a model file. `sandbox/models/` is in .gitignore."""
    mod = _mod()
    args = mod.parse_args(
        ["--start", "2025-01-01", "--end", "2026-01-01", "--seed", "7"]
    )
    assert Path("sandbox/models") in Path(args.out).parents
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert "sandbox/models/" in ignored


async def test_fit_gets_the_cached_candles_and_the_cli_arguments(
    wired: SimpleNamespace,
) -> None:
    args = wired.mod.parse_args(
        [
            "--start",
            "2025-01-01",
            "--end",
            "2026-01-01",
            "--tickers",
            "SBER",
            "--horizon-days",
            "12",
            "--folds",
            "4",
            "--seed",
            "7",
        ]
    )
    code = await wired.mod.body(args)
    assert code == 0
    kwargs = wired.calls["fit"]
    assert list(kwargs["candles_by_ticker"]) == ["SBER"]
    assert len(kwargs["candles_by_ticker"]["SBER"]) == 20
    assert kwargs["horizon_days"] == 12
    assert kwargs["folds"] == 4
    assert kwargs["seed"] == 7


async def test_tickers_default_to_the_configured_watchlist(
    wired: SimpleNamespace,
) -> None:
    args = wired.mod.parse_args(
        ["--start", "2025-01-01", "--end", "2026-01-01", "--seed", "7"]
    )
    await wired.mod.body(args)
    assert [call[0] for call in wired.calls["load"]] == list(wired.cfg.watchlist)


async def test_the_bundle_is_exported_to_the_requested_path(
    wired: SimpleNamespace, tmp_path: Path
) -> None:
    out = tmp_path / "model.joblib"
    args = wired.mod.parse_args(
        [
            "--start",
            "2025-01-01",
            "--end",
            "2026-01-01",
            "--seed",
            "7",
            "--out",
            str(out),
        ]
    )
    await wired.mod.body(args)
    assert wired.calls["export"] == (wired.model, out)


async def test_no_candles_at_all_reports_and_does_not_fit(
    wired: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _load(*_args: object, **_kwargs: object) -> list[Candle]:
        return []

    monkeypatch.setattr(wired.mod, "load", _load)
    args = wired.mod.parse_args(
        ["--start", "2025-01-01", "--end", "2026-01-01", "--seed", "7"]
    )
    code = await wired.mod.body(args)
    assert code == 1
    assert wired.calls["fit"] is None
    assert "no candles" in capsys.readouterr().out.lower()


def test_summary_reports_each_fold_rather_than_an_average() -> None:
    """Averaging hides a model that works in one regime and fails in another."""
    mod = _mod()
    text = mod.summary(_model(), Path("sandbox/models/model.joblib"))
    assert "0.61" in text
    assert "0.48" in text
    assert "0.73" in text
    assert "7" in text
    assert ", ".join(FEATURE_NAMES) in text


async def test_the_run_reports_no_token(
    wired: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    args = wired.mod.parse_args(
        ["--start", "2025-01-01", "--end", "2026-01-01", "--seed", "7"]
    )
    await wired.mod.body(args)
    out = capsys.readouterr().out
    assert "SECRET-TOKEN-VALUE" not in out
    assert "SECRET-TELEGRAM-TOKEN" not in out
