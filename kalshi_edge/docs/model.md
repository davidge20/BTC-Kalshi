## Probability + EV model (`p_model`, `sigma_blend`, `edge_pp`)

### Executive summary

- The core modeled quantity is a short-horizon threshold probability:
  \[
  p_{\text{model}}=\mathbb{P}(S_T \ge K)
  \]
  where \(S_T\) is BTC spot at Kalshi close and \(K\) is the ladder strike.
- We use a **driftless lognormal** baseline (fast + auditable) with time-to-close and an annualized volatility input \(\sigma\).
- In live evaluation, \(\sigma\) is `sigma_blend`: a simple **blend** of:
  - Deribit near-ATM implied vol, and
  - Coinbase 1-minute realized vol over ~1 hour.
- EV is **buy-only** and **per contract**, net of a simplified flat fee `FEE_CENTS`.
- The trader’s `edge_pp` is this net EV in dollars/contract (numerically equal to “probability points” because payoff is \$1).

### Definitions (units match the code)

- \(S_0\): underlying spot now (USD)
- \(K\): strike (USD)
- `minutes_left`: time to Kalshi close (minutes)
- \(t\): time to close (years)
- \(\sigma\): annualized volatility (decimal, e.g. 0.85 = 85%)
- `p_model`: modeled probability \(\mathbb{P}(S_T \ge K)\)

Time conversion uses `kalshi_edge/constants.py::MINUTES_PER_YEAR`:

\[
t = \frac{\texttt{minutes\_left}}{\texttt{MINUTES\_PER\_YEAR}}
\qquad
\texttt{MINUTES\_PER\_YEAR}=365\cdot 24\cdot 60=525{,}600
\]

### Baseline model: driftless lognormal

Implemented in `kalshi_edge/math_models.py::lognormal_prob_above`.

Assumption:

\[
\ln\left(\frac{S_T}{S_0}\right)\sim\mathcal{N}(0,\sigma^2 t)
\]

Then:

\[
p_{\text{model}}
=\mathbb{P}(S_T \ge K)
=1-\Phi\!\left(\frac{\ln(K/S_0)}{\sigma\sqrt{t}}\right)
=\Phi(d_2)
\]

with:

\[
d_2 = \frac{\ln(S_0/K)}{\sigma\sqrt{t}}
\]

\(\Phi(\cdot)\) is the standard normal CDF (implemented via `erf`).

### Volatility input: live `sigma_blend` vs backtest approximation

#### Live evaluation (`sigma_blend`)

Built in `kalshi_edge/market_state.py::build_market_state`:

- **Spot**: Deribit index (`deribit_index_price`)
- **Implied vol** (\(\sigma_{\text{implied}}\)): Deribit options, near-ATM median within a strike band controlled by `strategy.IV_BAND_PCT`
- **Realized vol** (\(\sigma_{\text{realized}}\)): Coinbase BTC-USD 1-minute candles (last 61 closes), annualized by \(\sqrt{\texttt{MINUTES\_PER\_YEAR}}\)
- **Blend rule** (`blend_vol`):
  - if \(\sigma_{\text{implied}}\le 0\): use realized
  - if \(\sigma_{\text{realized}}/\sigma_{\text{implied}} > 1.5\): 50/50
  - else: 70/30 (implied/realized)

#### Backtests (realized-only)

The minute-cadence backtest (`kalshi_edge/backtest_engine.py`) uses **Coinbase realized vol only** at each minute. There is no Deribit IV term in the backtest harness today, so backtest `sigma` should be read as a practical approximation for fast iteration.

### Mapping probability to trade decision (YES/NO in cents, fees, EV)

#### Kalshi price conventions

- A binary contract settles to **\$1** if it wins and **\$0** otherwise.
- Prices are integer **cents** in \([0,100]\). Convert cents \(\to\) dollars by dividing by 100.

#### Executable entry price used by the live evaluator

Kalshi orderbooks can have missing/stale asks. In live evaluation, we use a reciprocal-bid “buy-now proxy” from `kalshi_edge/ladder_eval.py::parse_orderbook_stats`:

\[
\texttt{Ybuy} = 100 - \texttt{Nbid}
\qquad
\texttt{Nbuy} = 100 - \texttt{Ybid}
\]

These proxy cents are what EV is computed against in the ladder table.

#### Fee treatment (as implemented)

The current code uses a flat per-contract fee `FEE_CENTS` (integer cents). EV and trader decisions treat the all-in entry cost as:

\[
\text{entry\_cost} = \frac{\text{price\_cents} + \texttt{FEE\_CENTS}}{100}
\]

#### EV formulas (buy-only, dollars per contract)

Let \(p_{\text{win}}\) be the side-specific win probability:

- YES: \(p_{\text{win}} = p_{\text{model}}\)
- NO: \(p_{\text{win}} = 1-p_{\text{model}}\)

Then, with an executable price \(c\) in cents:

\[
\mathrm{EV} = p_{\text{win}} - \frac{c + \texttt{FEE\_CENTS}}{100}
\]

Live evaluator uses \(c=\texttt{Ybuy}\) for YES and \(c=\texttt{Nbuy}\) for NO.

#### `edge_pp`

In the trading engine, `edge_pp` is this same net EV (dollars/contract). Because payoff is \$1, **EV in dollars equals probability points** on \([0,1]\) (e.g. \(0.03\) dollars \(\approx\) 3pp).

### Entry logic summary (high level)

The trader (`kalshi_edge/trader_v2_engine.py`) builds YES/NO candidates per strike and applies:

- **Minimum EV threshold**:
  - new entries require `edge_pp >= strategy.MIN_EV`
  - scale-ins (if enabled) require `edge_pp >= strategy.SCALE_IN_MIN_EV`
- **Liquidity gates**:
  - `strategy.SPREAD_MAX_CENTS` (skip wide spreads)
  - `strategy.MIN_TOP_SIZE` (skip thin top-of-book)
- **Caps**:
  - `MAX_COST_PER_EVENT`, `MAX_COST_PER_MARKET`
  - `MAX_POSITIONS_PER_EVENT`, `MAX_CONTRACTS_PER_MARKET`
  - `MAX_ENTRIES_PER_TICK` and `ORDER_SIZE`
- **Dedupe / scale-in semantics**:
  - `DEDUPE_MARKETS=true` enforces one entry per market (and disables scaling)
  - `ALLOW_SCALE_IN`, `SCALE_IN_COOLDOWN_SECONDS` control additional entries
- **Order mode** (`strategy.ORDER_MODE`):
  - `taker_only`: submit fill-or-kill at the “ask proxy”
  - `maker_only`: place/refresh a resting bid (optionally `POST_ONLY`)
  - `hybrid`: choose the better of the two per candidate

### Why this could work (and why it could fail)

#### Could work

- Kalshi ladder quotes can be thin/stale and may lag a probability implied by deeper BTC markets.
- When liquidity is poor, cross-venue convergence can be slow, creating temporary EV-ranked opportunities.

#### Could fail

- **Model miscalibration**: jump risk, tails, skew, and ultra-short-horizon effects can break the lognormal approximation.
- **Vol mismatch**: \(\sigma_{\text{blend}}\) mixes horizons (option expiries vs 1h realized) and is a heuristic.
- **Adverse selection**: you may only get filled when the market has moved against your model.
- **Fees + microstructure**: flat fee + proxy execution are approximations; small edges can disappear in practice.
- **Thin exits**: settlement is guaranteed; exits before settlement may not be (and are not the focus of the current engine).

