"""
strategy_config.py

Workflow B config loader: keep live-strategy and backtest tunables in one JSON
selected via `KALSHI_EDGE_CONFIG_JSON`.

Usage:

    export KALSHI_EDGE_CONFIG_JSON=/path/to/config.json
    python3 -m kalshi_edge.run --watch
    python3 -m kalshi_edge.backtesting.backtest

Preferred two-section JSON:

{
  "strategy": {
    "MIN_EV": 0.05,
    "ORDER_SIZE": 1,
    "MAX_COST_PER_EVENT": 5.0,
    "MAX_POSITIONS_PER_EVENT": 10,
    "MAX_COST_PER_MARKET": 1.0,
    "MAX_CONTRACTS_PER_MARKET": 2,
    "MIN_TOP_SIZE": 1.0,
    "SPREAD_MAX_CENTS": 30,
    "DEDUPE_MARKETS": false,
    "ALLOW_SCALE_IN": true,
    "SCALE_IN_COOLDOWN_SECONDS": 120,
    "SCALE_IN_MIN_EV": 0.06,
    "MAX_ENTRIES_PER_TICK": 1,
    "FEE_CENTS": 1,
    "ORDER_MODE": "taker_only",
    "POST_ONLY": true,
    "ORDER_REFRESH_SECONDS": 10,
    "CANCEL_STALE_SECONDS": 60,
    "P_REQUOTE_PP": 0.02,
    "paper": {
      "simulate_maker_fills": false,
      "tick_seconds": 1,
      "min_top_time_seconds": 3,
      "fill_prob_per_tick": 0.15,
      "partial_fill": true,
      "max_fill_per_tick": 1,
      "slippage_cents": 0,
      "seed": 12345
    }
  },
  "backtest": {
    "SERIES_TICKER": "KXBTCD",
    "DAYS": 14,
    "START_DATE": null,
    "END_DATE": null,
    "MAX_EVENTS": 50,
    "STEP_MINUTES": 1,
    "MAX_STRIKES": 120,
    "BAND_PCT": 25.0,
    "CACHE_DIR": "data/cache",
    "LOG_DIR": "backtests"
  }
}

Backward compatibility:
None. The config must use the canonical top-level `"strategy"` / `"backtest"` objects.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date
from typing import Any, Dict, List, Optional, Tuple, Type, Union, get_args, get_origin, get_type_hints


ENV_VAR = "KALSHI_EDGE_CONFIG_JSON"


def _warn(msg: str) -> None:
    print(f"[config] warning: {msg}", file=sys.stderr)


def _invalid(field: str, expected: str, value: Any) -> ValueError:
    return ValueError(f"Invalid config field {field}: expected {expected}, got {value} ({type(value).__name__})")


def _coerce_bool(field: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "t", "1", "yes", "y"}:
            return True
        if s in {"false", "f", "0", "no", "n"}:
            return False
    raise _invalid(field, "bool", value)


def _coerce_int(field: str, value: Any) -> int:
    if isinstance(value, bool):
        raise _invalid(field, "int", value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise _invalid(field, "int", value)
    if isinstance(value, str):
        s = value.strip()
        try:
            if s.lower().startswith(("0x", "+0x", "-0x")):
                return int(s, 16)
            return int(s, 10)
        except Exception:
            raise _invalid(field, "int", value)
    raise _invalid(field, "int", value)


def _coerce_float(field: str, value: Any) -> float:
    if isinstance(value, bool):
        raise _invalid(field, "float", value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        try:
            return float(s)
        except Exception:
            raise _invalid(field, "float", value)
    raise _invalid(field, "float", value)


def _coerce_str(field: str, value: Any) -> str:
    if isinstance(value, str):
        return value
    raise _invalid(field, "str", value)


@dataclass
class PaperConfig:
    simulate_maker_fills: bool = False
    tick_seconds: float = 1.0
    min_top_time_seconds: float = 3.0
    fill_prob_per_tick: float = 0.15
    partial_fill: bool = True
    max_fill_per_tick: int = 1
    slippage_cents: int = 0
    seed: Optional[int] = None

    def validate(self) -> None:
        if float(self.tick_seconds) < 0:
            raise ValueError("paper.tick_seconds must be >= 0")
        if float(self.min_top_time_seconds) < 0:
            raise ValueError("paper.min_top_time_seconds must be >= 0")
        p = float(self.fill_prob_per_tick)
        if p < 0 or p > 1:
            raise ValueError("paper.fill_prob_per_tick must be in [0, 1]")
        if int(self.max_fill_per_tick) < 1:
            raise ValueError("paper.max_fill_per_tick must be >= 1")
        if int(self.slippage_cents) < 0:
            raise ValueError("paper.slippage_cents must be >= 0")


@dataclass
class StrategyConfig:
    # --- core entry + caps ---
    MIN_EV: float = 0.05
    ORDER_SIZE: int = 1
    MAX_COST_PER_EVENT: float = 5.0
    MAX_POSITIONS_PER_EVENT: int = 5
    MAX_COST_PER_MARKET: float = 2.0
    MAX_CONTRACTS_PER_MARKET: int = 1

    # --- liquidity / data quality gates ---
    MIN_TOP_SIZE: float = 1.0
    SPREAD_MAX_CENTS: int = 30

    # --- scaling / dedupe semantics ---
    DEDUPE_MARKETS: bool = True
    ALLOW_SCALE_IN: bool = False
    SCALE_IN_COOLDOWN_SECONDS: int = 60
    SCALE_IN_MIN_EV: float = 0.06  # default MIN_EV + 0.01 (adjusted if MIN_EV overridden and SCALE_IN_MIN_EV absent)
    MAX_ENTRIES_PER_TICK: int = 1

    # --- logging knobs ---
    LOG_TOP_N_CANDIDATES: int = 5

    # --- evaluation / API budget knobs ---
    # Limits number of Kalshi orderbooks fetched per tick (closest-to-spot strikes).
    MAX_STRIKES: int = 10

    # --- fees ---
    FEE_CENTS: int = 1

    # --- maker logic (used by v2 engine) ---
    ORDER_MODE: str = "hybrid"  # "taker_only"|"maker_only"|"hybrid"
    POST_ONLY: bool = True
    ORDER_REFRESH_SECONDS: int = 10
    CANCEL_STALE_SECONDS: int = 60
    P_REQUOTE_PP: float = 0.02

    # --- evaluation / runtime ---
    REFRESH_SECONDS: int = 10
    WINDOW_MINUTES: int = 70
    BAND_PCT: float = 25.0
    SORT_MODE: str = "ev"  # "ev"|"strike"|"sens"
    DEPTH_WINDOW_CENTS: int = 2
    THREADS: int = 10
    IV_BAND_PCT: float = 3.0
    MIN_MINUTES_LEFT: float = 2.0

    # --- behavior flags ---
    LOCK_EVENT: bool = True
    LOG_SETTLEMENT: bool = False

    # --- logging paths (optional; CLI/env vars also accepted) ---
    TRADE_LOG_DIR: Optional[str] = None

    # --- paper trading / simulation (nested JSON object under key "paper") ---
    paper: PaperConfig = field(default_factory=PaperConfig)

    def validate(self, *, warnings: Optional[List[str]] = None) -> None:
        w = warnings if warnings is not None else []

        if float(self.MIN_EV) < 0:
            raise ValueError("MIN_EV must be >= 0")
        if int(self.ORDER_SIZE) < 1:
            raise ValueError("ORDER_SIZE must be >= 1")
        if int(self.MAX_ENTRIES_PER_TICK) < 1:
            raise ValueError("MAX_ENTRIES_PER_TICK must be >= 1")
        if int(self.LOG_TOP_N_CANDIDATES) < 0:
            raise ValueError("LOG_TOP_N_CANDIDATES must be >= 0")
        if int(self.MAX_STRIKES) < 1:
            raise ValueError("MAX_STRIKES must be >= 1")
        if int(self.MAX_CONTRACTS_PER_MARKET) < 1:
            raise ValueError("MAX_CONTRACTS_PER_MARKET must be >= 1")
        if float(self.SCALE_IN_MIN_EV) < float(self.MIN_EV):
            raise ValueError("SCALE_IN_MIN_EV must be >= MIN_EV")

        if float(self.MAX_COST_PER_EVENT) < 0:
            raise ValueError("MAX_COST_PER_EVENT must be >= 0")
        if float(self.MAX_COST_PER_MARKET) < 0:
            raise ValueError("MAX_COST_PER_MARKET must be >= 0")
        if int(self.MAX_POSITIONS_PER_EVENT) < 0:
            raise ValueError("MAX_POSITIONS_PER_EVENT must be >= 0")
        if int(self.FEE_CENTS) < 0:
            raise ValueError("FEE_CENTS must be >= 0")
        if float(self.MIN_TOP_SIZE) < 0:
            raise ValueError("MIN_TOP_SIZE must be >= 0")
        if int(self.SPREAD_MAX_CENTS) < 0:
            raise ValueError("SPREAD_MAX_CENTS must be >= 0")

        if bool(self.DEDUPE_MARKETS) and bool(self.ALLOW_SCALE_IN):
            # Consistent semantics: DEDUPE_MARKETS means one entry per market, so scaling is disabled.
            self.ALLOW_SCALE_IN = False
            w.append("DEDUPE_MARKETS=true forces ALLOW_SCALE_IN=false (one entry per market)")

        if self.ORDER_MODE not in {"taker_only", "maker_only", "hybrid"}:
            raise ValueError('ORDER_MODE must be one of: "taker_only", "maker_only", "hybrid"')

        if int(self.REFRESH_SECONDS) < 1:
            raise ValueError("REFRESH_SECONDS must be >= 1")
        if int(self.WINDOW_MINUTES) < 1:
            raise ValueError("WINDOW_MINUTES must be >= 1")
        if float(self.BAND_PCT) <= 0:
            raise ValueError("BAND_PCT must be > 0")
        if self.SORT_MODE not in {"ev", "strike", "sens"}:
            raise ValueError('SORT_MODE must be one of: "ev", "strike", "sens"')
        if int(self.DEPTH_WINDOW_CENTS) < 0:
            raise ValueError("DEPTH_WINDOW_CENTS must be >= 0")
        if int(self.THREADS) < 1:
            raise ValueError("THREADS must be >= 1")
        if float(self.IV_BAND_PCT) < 0:
            raise ValueError("IV_BAND_PCT must be >= 0")
        if float(self.MIN_MINUTES_LEFT) < 0:
            raise ValueError("MIN_MINUTES_LEFT must be >= 0")

        if not isinstance(self.paper, PaperConfig):
            raise ValueError("paper must be an object")
        self.paper.validate()

        for msg in w:
            _warn(msg)


@dataclass
class BacktestConfig:
    SERIES_TICKER: str = "KXBTCD"
    DAYS: int = 14
    START_DATE: Optional[str] = None
    END_DATE: Optional[str] = None
    EVENTS: Optional[str] = None
    MAX_EVENTS: int = 50
    STEP_MINUTES: int = 1
    MAX_STRIKES: int = 120
    BAND_PCT: float = 25.0
    ONLY_LAST_N_MINUTES: Optional[int] = None
    CACHE_DIR: str = "data/cache"
    LOG_DIR: str = "backtests"
    DEBUG_HTTP: bool = False

    def validate(self) -> None:
        if not isinstance(self.SERIES_TICKER, str) or not self.SERIES_TICKER.strip():
            raise ValueError("SERIES_TICKER must be a non-empty string")
        if int(self.DAYS) < 1:
            raise ValueError("DAYS must be >= 1")
        if int(self.MAX_EVENTS) < 1:
            raise ValueError("MAX_EVENTS must be >= 1")
        if int(self.STEP_MINUTES) < 1:
            raise ValueError("STEP_MINUTES must be >= 1")
        if int(self.MAX_STRIKES) < 1:
            raise ValueError("MAX_STRIKES must be >= 1")
        if float(self.BAND_PCT) <= 0:
            raise ValueError("BAND_PCT must be > 0")
        if self.ONLY_LAST_N_MINUTES is not None and int(self.ONLY_LAST_N_MINUTES) < 1:
            raise ValueError("ONLY_LAST_N_MINUTES must be >= 1 when set")
        if not isinstance(self.CACHE_DIR, str) or not self.CACHE_DIR.strip():
            raise ValueError("CACHE_DIR must be a non-empty string")
        if not isinstance(self.LOG_DIR, str) or not self.LOG_DIR.strip():
            raise ValueError("LOG_DIR must be a non-empty string")

        start = self.START_DATE
        end = self.END_DATE
        if (start is None) != (end is None):
            raise ValueError("START_DATE and END_DATE must be both set or both omitted")
        if start is not None:
            try:
                sdt = date.fromisoformat(str(start))
                edt = date.fromisoformat(str(end))
            except Exception:
                raise ValueError("START_DATE/END_DATE must be YYYY-MM-DD")
            if edt < sdt:
                raise ValueError("END_DATE must be >= START_DATE")


DEFAULT_CONFIG = StrategyConfig()
DEFAULT_BACKTEST_CONFIG = BacktestConfig()


def _field_type_map_for(cls: Type[Any]) -> Dict[str, Type[Any]]:
    try:
        return dict(get_type_hints(cls, globalns=globals(), localns=globals()))
    except Exception:
        return {f.name: f.type for f in fields(cls)}


def _field_type_map() -> Dict[str, Type[Any]]:
    # With `from __future__ import annotations`, dataclass field types may be strings.
    # Resolve them via get_type_hints so coercion works consistently.
    return _field_type_map_for(StrategyConfig)


def _backtest_field_type_map() -> Dict[str, Type[Any]]:
    return _field_type_map_for(BacktestConfig)


def _coerce_optional_int(field: str, value: Any) -> Optional[int]:
    if value is None:
        return None
    return _coerce_int(field, value)


def _coerce_optional_float(field: str, value: Any) -> Optional[float]:
    if value is None:
        return None
    return _coerce_float(field, value)


def _coerce_optional_str(field: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    return _coerce_str(field, value)


def _coerce_value(field: str, t: Any, raw: Any) -> Any:
    if t is bool:
        return _coerce_bool(field, raw)
    if t is int:
        return _coerce_int(field, raw)
    if t is float:
        return _coerce_float(field, raw)
    if t is str:
        return _coerce_str(field, raw)

    origin = get_origin(t)
    args = get_args(t)
    # Optional[T] is represented as Union[T, NoneType]
    if origin is Union and args and type(None) in args:
        inner = next((a for a in args if a is not type(None)), None)
        if inner is int:
            return _coerce_optional_int(field, raw)
        if inner is float:
            return _coerce_optional_float(field, raw)
        if inner is str:
            return _coerce_optional_str(field, raw)
    raise ValueError(f"Unsupported config field type for {field}: {t}")


def _apply_overrides_dataclass(obj: Any, overrides: Dict[str, Any], *, prefix: str) -> Any:
    if not is_dataclass(obj):
        raise ValueError(f"{prefix} must be a dataclass")
    out = obj
    cls = type(obj)
    try:
        tmap = dict(get_type_hints(cls, globalns=globals(), localns=globals()))
    except Exception:
        tmap = {f.name: f.type for f in fields(obj)}
    for k, raw in overrides.items():
        if k not in tmap:
            continue
        t = tmap[k]
        if is_dataclass(getattr(out, k, None)) or (isinstance(t, type) and is_dataclass(t)):
            # Nested dataclass is not used right now; keep simple.
            raise ValueError(f"Nested config object not supported at {prefix}.{k}")
        v = _coerce_value(f"{prefix}.{k}", t, raw)
        out = replace(out, **{k: v})
    return out


def _apply_overrides(base: StrategyConfig, overrides: Dict[str, Any], *, present_keys: set[str]) -> StrategyConfig:
    # Coerce values by declared field type.
    tmap = _field_type_map()
    out = base
    for k, raw in overrides.items():
        if k not in tmap:
            continue
        t = tmap[k]
        if isinstance(t, type) and is_dataclass(t):
            if not isinstance(raw, dict):
                raise _invalid(k, "object", raw)
            if k == "paper":
                out = replace(out, paper=_apply_overrides_dataclass(out.paper, {str(kk): vv for kk, vv in raw.items() if isinstance(kk, str)}, prefix="paper"))
            else:
                raise ValueError(f"Unsupported nested config object: {k}")
            continue

        v = _coerce_value(k, t, raw)
        out = replace(out, **{k: v})

    # Special defaulting: if MIN_EV is overridden but SCALE_IN_MIN_EV missing, derive it.
    if "MIN_EV" in present_keys and "SCALE_IN_MIN_EV" not in present_keys:
        out = replace(out, SCALE_IN_MIN_EV=float(out.MIN_EV) + 0.01)

    return out


def _read_config_json_from_env() -> Optional[Dict[str, Any]]:
    path = os.environ.get(ENV_VAR)
    if not path:
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"{ENV_VAR} points to missing file: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path}: {e.msg} (line {e.lineno}, col {e.colno})")
    except Exception as e:
        raise ValueError(f"Failed to read {path}: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"Config JSON must be an object/dict at top-level (got {type(data).__name__})")
    return {str(k): v for k, v in data.items() if isinstance(k, str)}


def _extract_strategy_source(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Config must have top-level 'strategy'; only use that object for strategy fields.
    """
    strategy_obj = data.get("strategy")
    if isinstance(strategy_obj, dict):
        return {str(k): v for k, v in strategy_obj.items() if isinstance(k, str)}
    raise ValueError('Config JSON must contain a top-level "strategy" object')


def _extract_backtest_source(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    If config has top-level 'backtest', only use that object for backtest fields.
    Otherwise return empty dict (defaults will be used).
    """
    backtest_obj = data.get("backtest")
    if isinstance(backtest_obj, dict):
        return {str(k): v for k, v in backtest_obj.items() if isinstance(k, str)}
    return {}


def load_config() -> StrategyConfig:
    """
    Load StrategyConfig from JSON path in env var KALSHI_EDGE_CONFIG_JSON.

    - If env var unset: return DEFAULT_CONFIG.
    - Config must have top-level "strategy": use only that dict.
    """
    data = _read_config_json_from_env()
    if data is None:
        cfg = replace(DEFAULT_CONFIG)
        cfg.validate()
        return cfg

    source = _extract_strategy_source(data)
    known = set(_field_type_map().keys())
    present = set(source.keys())

    unknown = sorted([k for k in present if k not in known])
    if unknown:
        _warn(f"Unknown keys ignored: {', '.join(unknown[:30])}" + (" ..." if len(unknown) > 30 else ""))

    cfg = _apply_overrides(replace(DEFAULT_CONFIG), source, present_keys=present)
    cfg.validate()
    return cfg


def load_backtest_config() -> BacktestConfig:
    """
    Load BacktestConfig from JSON path in env var KALSHI_EDGE_CONFIG_JSON.

    Source priority:
    1) top-level "backtest" object (preferred)
    2) defaults
    """
    data = _read_config_json_from_env()
    if data is None:
        cfg = replace(DEFAULT_BACKTEST_CONFIG)
        cfg.validate()
        return cfg

    source = _extract_backtest_source(data)
    if not source:
        cfg = replace(DEFAULT_BACKTEST_CONFIG)
        cfg.validate()
        return cfg

    tmap = _backtest_field_type_map()
    known = set(tmap.keys())
    present = set(source.keys())
    unknown = sorted([k for k in present if k not in known])
    if unknown:
        _warn(f"Unknown backtest keys ignored: {', '.join(unknown[:30])}" + (" ..." if len(unknown) > 30 else ""))

    out = replace(DEFAULT_BACKTEST_CONFIG)
    for k, raw in source.items():
        if k not in tmap:
            continue
        t = tmap[k]
        out = replace(out, **{k: _coerce_value(f"backtest.{k}", t, raw)})

    out.validate()
    return out


def config_source_path() -> Optional[str]:
    """
    Return the config JSON path (if any) used to load config.
    """
    p = os.environ.get(ENV_VAR)
    return str(p) if p else None


def config_to_dict(cfg: StrategyConfig) -> Dict[str, Any]:
    """
    Convert StrategyConfig into a plain JSON-serializable dict (including nested paper config).
    """
    if not isinstance(cfg, StrategyConfig):
        raise TypeError(f"cfg must be a StrategyConfig (got {type(cfg).__name__})")
    out: Dict[str, Any] = {}
    for k in cfg.__dataclass_fields__.keys():
        v = getattr(cfg, k)
        if is_dataclass(v):
            out[k] = {kk: getattr(v, kk) for kk in v.__dataclass_fields__.keys()}
        else:
            out[k] = v
    return out


def config_hash(cfg: StrategyConfig) -> str:
    """
    Stable sha256 of config fields (sorted JSON, compact separators).
    """
    payload = config_to_dict(cfg)
    b = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b).hexdigest()



def _self_test() -> None:
    sample = {
        "MIN_EV": "0.05",
        "ORDER_SIZE": "2",
        "DEDUPE_MARKETS": "false",
        "ALLOW_SCALE_IN": "true",
        "SCALE_IN_MIN_EV": "0.06",
        "P_REQUOTE_PP": "0.02",
    }
    cfg = _apply_overrides(replace(DEFAULT_CONFIG), sample, present_keys=set(sample.keys()))
    cfg.validate()
    assert isinstance(cfg.MIN_EV, float)
    assert isinstance(cfg.ORDER_SIZE, int) and cfg.ORDER_SIZE == 2
    assert cfg.DEDUPE_MARKETS is False
    assert cfg.ALLOW_SCALE_IN is True


if __name__ == "__main__":
    _self_test()
    print("[config] self-test OK")

