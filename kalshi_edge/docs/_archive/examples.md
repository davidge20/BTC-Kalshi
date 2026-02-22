# Examples (implementation-traced)

## Quick start
From the **parent directory** (the folder that contains `kalshi_edge/`):

```bash
python3 -m kalshi_edge.run
```

If discovery fails (no event found within the default closing-soon window), widen the window:

```bash
python3 -m kalshi_edge.run --window-minutes 360
# or:
python3 -m kalshi_edge.run --window-minutes 1440
```

To see HTTP debug output:

```bash
python3 -m kalshi_edge.run --debug-http
```

## Common run modes

### Single snapshot (default)
```bash
python3 -m kalshi_edge.run
```

### Watch mode (refresh repeatedly)
```bash
python3 -m kalshi_edge.run --watch --refresh-seconds 10
```

### Evaluate more strikes
```bash
python3 -m kalshi_edge.run --max-strikes 200
```

### Sort output
```bash
python3 -m kalshi_edge.run --sort ev
```

Valid `--sort` choices are defined in `kalshi_edge/run.py` as: `ev`, `sens`, `strike`.

### Evaluate a specific event or URL

Both are supported by `kalshi_edge/run.py` (it passes them into `kalshi_edge/pipeline.py::evaluate_event`):

```bash
python3 -m kalshi_edge.run --event "KXBTCD-26FEB0518"
```

```bash
python3 -m kalshi_edge.run --url "https://kalshi.com/markets/kxbtcd/bitcoin-price-abovebelow/kxbtcd-26feb0518"
```

### Trading mode (V1)

Trading is enabled with `--trade`, and `run.py` wires this to `kalshi_edge/trader_v1.py::V1Trader`.

You must supply credentials (or set env vars), as enforced in `kalshi_edge/run.py`:

```bash
python3 -m kalshi_edge.run --trade \
  --api-key-id "$KALSHI_API_KEY_ID" \
  --private-key-path "$KALSHI_PRIVATE_KEY_PATH" \
  --dry-run
```

Useful knobs (all parsed in `kalshi_edge/run.py`):

```bash
python3 -m kalshi_edge.run --trade --dry-run \
  --trade-count 1 \
  --max-contracts 3 \
  --min-minutes-left 2.0 \
  --fee-cents 1
```

> TODO: Add a captured sample output table and a sample `trade_log.jsonl` snippet once available.
