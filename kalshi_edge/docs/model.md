## Probability + EV model (`p_model`, `sigma_blend`, `edge_pp`)

### Executive summary

- The core modeled quantity is a short-horizon threshold probability:
  \[
  p_{\text{model}}=\mathbb{P}(S_T \ge K)
  \]
  where \(S_T\) is BTC spot at Kalshi close and \(K\) is the ladder strike.
- We use a **driftless lognormal** baseline (fast + auditable) with an annualized volatility input \(\sigma\) and time-to-close \(t\).
- In live evaluation, \(\sigma\) is `sigma_blend`: a simple **blend** of:
  - Deribit near-ATM implied vol, and
  - Coinbase 1-minute realized vol over ~1 hour.
- EV is **buy-only** and **per contract**, net of a simplified flat fee `FEE_CENTS`.
- The trader’s `edge_pp` is this net EV in dollars/contract (numerically equal to “probability points” because payoff is \$1).

### Key assumptions (read this before using the numbers)

- **Driftless short-horizon lognormal**: the model treats log returns as Normal with mean 0 over the remaining horizon (no drift term).
- **Single constant volatility input**: \(\sigma\) is treated as one annualized number for the full horizon (no term structure or skew).
- **Heuristic \(\sigma\)**: live runs use `sigma_blend`, blending Deribit implied vol (options-implied) and Coinbase realized vol (historical). This is not a measure-consistent “true probability.”
- **Buy-only EV, flat fee**: EV is computed for entry only (no explicit exit model) and uses a flat per-contract `FEE_CENTS` approximation.
- **Executable price proxy**: live “buy-now” uses the reciprocal-bid proxy (`Ybuy=100-Nbid`, `Nbuy=100-Ybid`) because asks can be missing/stale.
- **Primary use is ranking/diagnostics**: treat small edges as fragile; microstructure, latency, and tails can dominate.

### Units and notation (matches the code)

- \(S_0\): underlying spot now (USD) from `kalshi_edge/market_state.py::deribit_index_price`
- \(K\): strike (USD) from `kalshi_edge/data/kalshi/models.py::market_strike_from_floor`
- `minutes_left`: time to Kalshi close (minutes) from `kalshi_edge/data/kalshi/models.py::above_markets_from_event`
- \(t\): time to close (years)
- \(\sigma\): annualized volatility (decimal, e.g. 0.85 = 85%)

Time conversion uses `kalshi_edge/constants.py::MINUTES_PER_YEAR`:

\[
t = \frac{\texttt{minutes\_left}}{\texttt{MINUTES\_PER\_YEAR}}
\qquad
\texttt{MINUTES\_PER\_YEAR}=365\cdot 24\cdot 60=525{,}600
\]

### Modeling context: lognormal baseline (GBM intuition)

A standard starting point is geometric Brownian motion (GBM) for \(S_t\):

\[
dS_t = \mu S_t\,dt + \sigma S_t\,dW_t.
\]

Applying Itô’s lemma gives:

\[
d(\ln S_t) = \left(\mu - \tfrac12\sigma^2\right)dt + \sigma\,dW_t.
\]

Under constant \(\sigma\), the terminal log return is normally distributed:

\[
\ln\!\left(\frac{S_T}{S_0}\right) \sim \mathcal{N}\!\left(\left(\mu - \tfrac12\sigma^2\right)t,\ \sigma^2 t\right).
\]

#### What the implementation assumes

The implementation uses a simplified “driftless” lognormal baseline:

\[
\ln\!\left(\frac{S_T}{S_0}\right)\sim \mathcal{N}(0,\sigma^2 t).
\]

For horizons on the order of minutes to hours, the drift contribution is typically small relative to the diffusion scale \(\sigma\sqrt{t}\). This also keeps the mapping from \(\sigma\) to \(p_{\text{model}}\) transparent.

#### A note on measure choice

In textbook derivative pricing one often works under a risk-neutral measure. We do not attempt to enforce a single consistent measure: we blend an implied-volatility signal (options-implied, largely risk-neutral information) with a realized-volatility signal (historical information). Treat \(p_{\text{model}}\) as a pragmatic short-horizon probability proxy.

### Baseline model: driftless lognormal

Implemented in `kalshi_edge/math_models.py::lognormal_prob_above`.

#### Derivation (matches the implementation)

We model the terminal log return over the remaining horizon as:

\[
X := \ln\!\left(\frac{S_T}{S_0}\right)\sim\mathcal{N}(0,\sigma^2 t)
\]

Then:

\[
p_{\text{model}}=\mathbb{P}(S_T\ge K)
=\mathbb{P}\!\left(X \ge \ln(K/S_0)\right)
\]

Standardizing \(Z=X/(\sigma\sqrt{t})\sim\mathcal{N}(0,1)\) yields:

\[
z=\frac{\ln(K/S_0)}{\sigma\sqrt{t}}
\qquad
p_{\text{model}} = \mathbb{P}(Z\ge z)=1-\Phi(z)
\]

The code expresses the same quantity via \(d_2=\ln(S_0/K)/(\sigma\sqrt{t})=-z\), so \(p_{\text{model}}=\Phi(d_2)\). \(\Phi(\cdot)\) is implemented using `erf`, and the final value is clamped to \([0,1]\).

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

#### Implied vol details (Deribit near-ATM median)

In `deribit_atm_implied_vol`, “near-ATM” is defined by a symmetric strike band around spot controlled by `strategy.IV_BAND_PCT`:

\[
\text{lo} = S_0\left(1 - \frac{\texttt{IV\_BAND\_PCT}}{100}\right)
\qquad
\text{hi} = S_0\left(1 + \frac{\texttt{IV\_BAND\_PCT}}{100}\right)
\]

The implementation filters option strikes to \([\text{lo},\text{hi}]\), groups by expiry, chooses a near expiry with enough samples, and takes the **median** IV for robustness.

#### Realized vol details (Coinbase 1-minute candles)

`coinbase_realized_vol_1h` computes 1-minute log returns \(r_i=\ln(C_i/C_{i-1})\), takes the population stdev \(\sigma_{1m}\), then annualizes:

\[
\sigma_{\text{realized}} = \sigma_{1m}\sqrt{\texttt{MINUTES\_PER\_YEAR}}
\]

#### Backtests (realized-only)

The minute-cadence backtest (`kalshi_edge/backtesting/backtest_engine.py`) uses **Coinbase realized vol only** at each minute. There is no Deribit IV term in the backtest harness today, so backtest `sigma` should be read as a practical approximation for fast iteration.

### Derived one-sigma move (diagnostic)

This quantity is a scale/intuition diagnostic: it summarizes the model-implied “typical” move over the remaining time horizon given \(\sigma_{\text{blend}}\). It is printed in the CLI summary but not used directly in \(p_{\text{model}}\) or EV.

Computed in `kalshi_edge/math_models.py::expected_one_sigma_move_pct`:

\[
\texttt{one\_sigma\_move\_pct} = \sigma_{\text{blend}}\sqrt{t}\cdot 100
\]

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

The trader (`kalshi_edge/trader_engine.py`) builds YES/NO candidates per strike and applies:

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

