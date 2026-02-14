# Examples

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

> TODO: Add a sample output table once captured.
