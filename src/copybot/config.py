"""Configuration loading and validation.

Every tunable lives in config.yaml. This module turns that file into a frozen
dataclass and refuses to start on a configuration that would silently produce
wrong numbers -- notably a zero fee fallback or mode: live.

No secrets are read from this file. Live trading will read credentials from
environment variables only (see NOTES.md).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed, or unsafe to run on."""


# Environment variables that LiveExecutor will need. Documented here so there
# is exactly one place listing them. Never read at import time in paper mode.
LIVE_ENV_VARS = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "POLYMARKET_PROXY_ADDRESS",
)


@dataclass(frozen=True)
class Config:
    target_wallet: str
    starting_capital_usd: float
    stake_per_copy_usd: float
    max_entry_price: float
    our_max_fill_price: float
    max_copies_per_token: int
    max_trade_age_seconds: int
    poll_interval_seconds: int
    activity_fetch_limit: int
    mirror_partial_sells: bool
    fee_rate_fallback: float
    fee_bps_override: float | None
    mode: str
    dashboard_port: int
    dashboard_host: str
    db_path: str
    log_path: str
    log_max_bytes: int
    log_backup_count: int
    resolution_check_interval_seconds: int
    equity_snapshot_interval_seconds: int
    mark_interval_seconds: int
    near_close_window_seconds: int
    mark_interval_near_close_seconds: int
    clv_max_spread: float
    max_book_lag_seconds: float
    shadow_ladder_usd: list
    shadow_ladder_include_his_sizes: bool
    clv_horizons_minutes: list
    shadow_band_max_price: float
    entry_mode: str
    limit_order_ttl_seconds: int
    limit_queue_model: str
    limit_fill_requires_sell_prints: bool
    stake_schedule: list
    stake_variants_usd: list
    vwap_breakeven_ratio: float
    kill_vwap_ratio: float
    kill_vwap_min_fills: int
    kill_depth_cost_pct: float
    kill_depth_min_signals: int
    kill_clv_min_copies: int
    kill_clv_horizon_minutes: int
    kill_capture_failure_pct: float
    pnl_verdict_min_resolved: int
    go_live_min_stable_days: int
    slippage_warn_pct: float
    respect_min_order_size: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- derived -----------------------------------------------------------
    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    def resolve_path(self, value: str, base: Path | None = None) -> Path:
        """Resolve a config path relative to the repo root unless absolute."""
        p = Path(value)
        if p.is_absolute():
            return p
        return (base or Path.cwd()) / p


_REQUIRED = (
    "target_wallet",
    "starting_capital_usd",
    "stake_per_copy_usd",
    "max_entry_price",
    "our_max_fill_price",
    "max_copies_per_token",
    "max_trade_age_seconds",
    "poll_interval_seconds",
    "mirror_partial_sells",
    "fee_rate_fallback",
    "mode",
    "dashboard_port",
)

_DEFAULTS: dict[str, Any] = {
    "fee_bps_override": None,
    "activity_fetch_limit": 100,
    "dashboard_host": "127.0.0.1",
    "db_path": "data/copybot.sqlite3",
    "log_path": "logs/copybot.log",
    "log_max_bytes": 10 * 1024 * 1024,
    "log_backup_count": 5,
    "resolution_check_interval_seconds": 300,
    "equity_snapshot_interval_seconds": 300,
    "slippage_warn_pct": 15.0,
    "respect_min_order_size": True,
    "mark_interval_seconds": 300,
    "near_close_window_seconds": 1800,
    "mark_interval_near_close_seconds": 60,
    "clv_max_spread": 0.05,
    "max_book_lag_seconds": 5.0,
    "shadow_ladder_usd": [1.00, 3.00, 10.00],
    "shadow_ladder_include_his_sizes": True,
    "clv_horizons_minutes": [15, 60, 360],
    "shadow_band_max_price": 0.50,
    "entry_mode": "market",
    "limit_order_ttl_seconds": 300,
    "limit_queue_model": "back",
    "limit_fill_requires_sell_prints": True,
    "stake_schedule": [0.34, 0.33, 0.33],
    "stake_variants_usd": [1.00, 2.00, 3.00],
    "vwap_breakeven_ratio": 1.395,
    "kill_vwap_ratio": 1.395,
    "kill_vwap_min_fills": 50,
    "kill_depth_cost_pct": 25.0,
    "kill_depth_min_signals": 50,
    "kill_clv_min_copies": 100,
    "kill_clv_horizon_minutes": 60,
    "kill_capture_failure_pct": 30.0,
    "pnl_verdict_min_resolved": 700,
    "go_live_min_stable_days": 30,
}


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config file must contain a YAML mapping at the top level")

    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise ConfigError(f"config missing required keys: {', '.join(sorted(missing))}")

    merged = {**_DEFAULTS, **data}
    known = {f for f in Config.__dataclass_fields__ if f != "raw"}
    unknown = set(merged) - known
    if unknown:
        # Loud rather than silent: a typo'd key must not read as a default.
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")

    cfg = Config(raw=dict(merged), **{k: merged[k] for k in known})
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.mode not in ("paper", "live"):
        raise ConfigError(f"mode must be 'paper' or 'live', got {cfg.mode!r}")

    if cfg.mode == "live":
        raise ConfigError(
            "mode: live is not implemented. Live execution is deliberately "
            "unbuilt -- only PaperExecutor exists. Set mode: paper to run."
        )

    wallet = cfg.target_wallet
    if not (isinstance(wallet, str) and wallet.startswith("0x") and len(wallet) == 42):
        raise ConfigError(f"target_wallet must be a 0x-prefixed 42-char address, got {wallet!r}")
    try:
        int(wallet, 16)
    except ValueError as exc:
        raise ConfigError(f"target_wallet is not valid hex: {wallet!r}") from exc

    # The guardrail that matters most: a zero or missing fee fallback turns a
    # failed gamma lookup into free trading, which makes paper beat live in
    # exactly the cheap-price band this wallet lives in.
    if cfg.fee_rate_fallback is None or cfg.fee_rate_fallback <= 0:
        raise ConfigError(
            "fee_rate_fallback must be > 0. A zero fallback means a missing fee "
            "field silently becomes free trading."
        )
    if cfg.fee_rate_fallback > 0.5:
        raise ConfigError(f"fee_rate_fallback looks wrong: {cfg.fee_rate_fallback}")

    if cfg.fee_bps_override is not None:
        if cfg.fee_bps_override < 0:
            raise ConfigError("fee_bps_override must be >= 0 or null")

    for name, lo, hi in (
        ("max_entry_price", 0.0, 1.0),
        ("our_max_fill_price", 0.0, 1.0),
    ):
        v = getattr(cfg, name)
        if not (lo < v <= hi):
            raise ConfigError(f"{name} must be in ({lo}, {hi}], got {v}")

    for name in ("starting_capital_usd", "stake_per_copy_usd"):
        if getattr(cfg, name) <= 0:
            raise ConfigError(f"{name} must be > 0")
    if cfg.stake_per_copy_usd > cfg.starting_capital_usd:
        raise ConfigError("stake_per_copy_usd cannot exceed starting_capital_usd")

    for name in (
        "max_copies_per_token",
        "max_trade_age_seconds",
        "poll_interval_seconds",
        "activity_fetch_limit",
        "resolution_check_interval_seconds",
        "equity_snapshot_interval_seconds",
        "mark_interval_seconds",
        "near_close_window_seconds",
        "mark_interval_near_close_seconds",
    ):
        if getattr(cfg, name) <= 0:
            raise ConfigError(f"{name} must be > 0")

    if cfg.entry_mode not in ("limit", "market"):
        raise ConfigError(f"entry_mode must be 'limit' or 'market', got {cfg.entry_mode!r}")
    if cfg.entry_mode == "limit":
        raise ConfigError(
            "entry_mode: limit is not wired into the buy path yet. limits.py "
            "implements and tests the resting-order model, but strategy.py "
            "still crosses the spread, so running in limit mode would silently "
            "market-buy. Leave it on 'market' until the wiring lands."
        )
    if cfg.limit_queue_model not in ("back", "front"):
        raise ConfigError(f"limit_queue_model must be 'back' or 'front', got "
                          f"{cfg.limit_queue_model!r}")
    if cfg.limit_order_ttl_seconds <= 0:
        raise ConfigError("limit_order_ttl_seconds must be > 0")
    if not cfg.stake_schedule or any(f <= 0 for f in cfg.stake_schedule):
        raise ConfigError("stake_schedule must be a non-empty list of positive fractions")
    if abs(sum(cfg.stake_schedule) - 1.0) > 1e-6:
        raise ConfigError(
            f"stake_schedule must sum to exactly 1.0, got {sum(cfg.stake_schedule)}. "
            "The per-token budget is a hard cap."
        )
    if cfg.max_copies_per_token > len(cfg.stake_schedule):
        raise ConfigError(
            f"max_copies_per_token ({cfg.max_copies_per_token}) exceeds the "
            f"{len(cfg.stake_schedule)} entries in stake_schedule"
        )
    if not cfg.stake_variants_usd or any(v <= 0 for v in cfg.stake_variants_usd):
        raise ConfigError("stake_variants_usd must be a non-empty list of positive amounts")
    if cfg.shadow_band_max_price < cfg.max_entry_price:
        raise ConfigError("shadow_band_max_price must be >= max_entry_price")
    if cfg.vwap_breakeven_ratio <= 1.0:
        raise ConfigError("vwap_breakeven_ratio must be > 1.0")

    if not cfg.shadow_ladder_usd or any(v <= 0 for v in cfg.shadow_ladder_usd):
        raise ConfigError("shadow_ladder_usd must be a non-empty list of positive amounts")
    if any(v <= 0 for v in cfg.clv_horizons_minutes):
        raise ConfigError("clv_horizons_minutes must all be positive")
    if cfg.max_book_lag_seconds < 0:
        raise ConfigError("max_book_lag_seconds must be >= 0")

    for name in ("kill_depth_cost_pct", "kill_depth_min_signals", "kill_clv_min_copies",
                 "kill_capture_failure_pct", "pnl_verdict_min_resolved",
                 "go_live_min_stable_days", "kill_clv_horizon_minutes"):
        if getattr(cfg, name) <= 0:
            raise ConfigError(f"{name} must be > 0")
    if cfg.kill_clv_horizon_minutes not in cfg.clv_horizons_minutes:
        raise ConfigError(
            f"kill_clv_horizon_minutes ({cfg.kill_clv_horizon_minutes}) must be one of "
            f"clv_horizons_minutes ({cfg.clv_horizons_minutes}), or the stopping rule "
            "has no data to check against"
        )

    if not (0 < cfg.dashboard_port < 65536):
        raise ConfigError(f"dashboard_port out of range: {cfg.dashboard_port}")

    # No login on the dashboard, so binding it off-localhost exposes the whole
    # bot to the internet. Refuse unless explicitly overridden by env var.
    if cfg.dashboard_host not in ("127.0.0.1", "localhost", "::1"):
        if os.environ.get("COPYBOT_ALLOW_PUBLIC_DASHBOARD") != "1":
            raise ConfigError(
                f"dashboard_host is {cfg.dashboard_host!r} but the dashboard has no "
                "authentication. Bind 127.0.0.1 and use an SSH tunnel (see README)."
            )
