# Probability Model

## Executive Summary

We estimate a short-horizon threshold probability
$
p_{\text{model}}=\mathbb{P}(S_T \ge K)
$
using a driftless lognormal baseline with an annualized volatility input $\sigma$ and time-to-close $t$. In practice we set $\sigma=\sigma_{\text{blend}}$, a simple blend of a near-ATM implied-volatility estimate (Deribit options) and a short-window realized-volatility estimate (Coinbase 1-minute candles). We use $p_{\text{model}}$ as an interpretable probability proxy for research-style EV ranking and diagnostics.


## Modeling Context: Lognormal Baseline and Brownian Motion

We use a tractable short-horizon model for $\mathbb{P}(S_T \ge K)$ that is easy to audit and connect to observable volatility inputs. A standard starting point in mathematical finance is geometric Brownian motion (GBM) for the asset price $S_t$:

$$
dS_t = \mu S_t\,dt + \sigma S_t\,dW_t.
$$

where $W_t$ is a standard Brownian motion, $\mu$ is a drift parameter, and $\sigma$ is the (annualized) volatility parameter. Applying Itô’s lemma gives the corresponding log-price dynamics:

$$
d(\ln S_t) = \left(\mu - \tfrac12\sigma^2\right)dt + \sigma\,dW_t.
$$

Under constant $\sigma$, this implies that the terminal log return is normally distributed:

$$
\ln\!\left(\frac{S_T}{S_0}\right) \sim \mathcal{N}\!\left(\left(\mu - \tfrac12\sigma^2\right)t,\ \sigma^2 t\right).
$$

### What the Implementation Assumes

The implementation uses a simplified “driftless” lognormal baseline:

$$
\ln\!\left(\frac{S_T}{S_0}\right) \sim \mathcal{N}(0,\sigma^2 t).
$$

Intuitively, for horizons on the order of minutes to hours, the drift contribution is typically small relative to the diffusion scale $\sigma\sqrt{t}$. This choice also keeps the mapping from $\sigma$ to threshold probabilities transparent, which is useful for a research tool whose main objective is ranking and diagnosing edge rather than producing a fully specified no-arbitrage valuation.

### A Note on Measure Choice

In a textbook derivative-pricing setting, one often works under a risk-neutral measure and would replace $\mu$ with $r-q$ (risk-free rate minus dividend or convenience yield) in the GBM drift. We do not attempt to enforce a single consistent measure: we blend an implied-volatility signal (options-implied, largely risk-neutral information) with a realized-volatility signal (historical, physical-measure information). The result should be read as a pragmatic short-horizon probability proxy.

## Key Assumptions

For clarity, the model used in this repo makes the following simplifying assumptions:

- **Lognormal diffusion baseline**: $\ln(S_T/S_0)$ is modeled as Normal over the horizon.
- **Zero drift over the horizon**: the mean log return is set to $0$.
- **Constant volatility parameter**: $\sigma$ is treated as a single annualized number used throughout the horizon.
- **Single-parameter implied-vol summary**: we summarize near-ATM option IVs using a median within a strike band around spot.
- **Research proxy, not a calibrated pricing model**: outputs are used for ranking/diagnostics rather than no-arbitrage valuation.

## Units and Notation

- $S_0$: current BTC spot price in USD (`kalshi_edge/market_state.py::deribit_index_price`)
- $K$: ladder strike in USD (`kalshi_edge/kalshi_api.py::market_strike_from_floor`)
- `minutes_left`: time-to-close in **minutes** (`kalshi_edge/kalshi_api.py::above_markets_from_event`)
- $t$: time-to-close in **years**
- $\sigma$: annualized volatility in **decimal** units (e.g. $0.85$ means $85\%$ annualized)

The constant `MINUTES_PER_YEAR` is defined in `kalshi_edge/constants.py`.

## Win Probability: Lognormal Baseline

Implemented in `kalshi_edge/math_models.py::lognormal_prob_above`.

The modeled probability is:

$$
p_{\text{model}} = \mathbb{P}(S_T \ge K)
$$

### Distributional Assumption

The code assumes a driftless lognormal model for short-horizon returns:

$$
\ln\!\left(\frac{S_T}{S_0}\right)\sim \mathcal{N}(0,\sigma^2 t)
$$

where time-to-close in years is:

$$
t = \frac{\texttt{minutes\_left}}{\texttt{MINUTES\_PER\_YEAR}}
$$

### Reducing the Event to a Normal Tail Probability

Start by rewriting the threshold event in log space:

$$
\mathbb{P}(S_T \ge K)
= \mathbb{P}\!\left(\ln S_T \ge \ln K\right)
= \mathbb{P}\!\left(\ln\!\left(\frac{S_T}{S_0}\right) \ge \ln\!\left(\frac{K}{S_0}\right)\right).
$$

Let

$$
X = \ln\!\left(\frac{S_T}{S_0}\right).
$$

Under the assumption above, $X \sim \mathcal{N}(0,\sigma^2 t)$. Standardize to a unit Normal by dividing by its standard deviation:

$$
Z = \frac{X}{\sigma\sqrt{t}} \sim \mathcal{N}(0,1).
$$

The strike threshold becomes the standardized value

$$
z = \frac{\ln(K/S_0)}{\sigma\sqrt{t}}.
$$

This is the origin of the $z$ used in the implementation: it is log-moneyness $\ln(K/S_0)$ expressed in units of the model’s one-standard-deviation move over horizon $t$. Here, **log-moneyness** is a log-scale measure of how far the strike $K$ is from the current spot $S_0$: it is positive when $K>S_0$, negative when $K<S_0$, and zero when $K=S_0$. We use the log ratio because the model is specified in terms of log returns, $\ln(S_T/S_0)$, so the strike comparison naturally becomes a threshold in log space, $\ln(K/S_0)$.

### Closed Form for $p_{\text{model}}$

Using the standardized variable $Z$, we have:

$$
p_{\text{model}}
= \mathbb{P}(X \ge \ln(K/S_0))
= \mathbb{P}\!\left(Z \ge z\right).
$$

Let $\Phi(\cdot)$ denote the standard normal CDF. Then:

$$
\mathbb{P}(Z \ge z) = 1 - \Phi(z),
$$

which yields the implemented closed form:

$$
p_{\text{model}} = \operatorname{clamp}_{[0,1]}\!\Bigl(1 - \Phi(z)\Bigr)
$$

where:
- $\Phi(\cdot)$ is the standard normal CDF implemented in `kalshi_edge/math_models.py::norm_cdf` using `erf`.
- `clamp01` is `kalshi_edge/math_models.py::clamp01`.

Edge cases in code:
- if `minutes_left <= 0`, it returns $1$ if $S_0 \ge K$ else $0$
- if $S_0 \le 0$ or $K \le 0$, it returns $0$
- if $\sigma\sqrt{t} \le 0$, it returns the same deterministic comparison result

## Volatility Inputs and Sigma Blend

All volatility sourcing and blending is implemented in `kalshi_edge/market_state.py::build_market_state`.

### Implied Volatility: Deribit

Implied vol comes from `kalshi_edge/market_state.py::deribit_atm_implied_vol`:

- Pull option summaries from Deribit.
- Filter option strikes within a band around spot.

The objective is to estimate a single **near-at-the-money (near-ATM) implied volatility level** using many option quotes whose strikes lie close to the current spot price. Rather than relying on a single strike (which may be illiquid, missing, or noisy), we:

- collect a *set* of option-implied volatilities from strikes near spot, and
- take a robust summary statistic (the median).

Concretely, we define a symmetric strike window around $S_0$:

$$
\text{lo} = S_0\left(1 - \frac{\texttt{iv\_band\_pct}}{100}\right)
\qquad
\text{hi} = S_0\left(1 + \frac{\texttt{iv\_band\_pct}}{100}\right)
$$

These bounds are exactly the “$\pm$ band” around spot. For example, if $\texttt{iv\_band\_pct}=3$, then we keep strikes in $[0.97S_0,\,1.03S_0]$.

In code, $\text{lo}$ and $\text{hi}$ are used to keep only option rows whose strike $K_{\text{opt}}$ satisfies:

$$
\text{lo} \le K_{\text{opt}} \le \text{hi}.
$$

This “near-ATM” restriction is primarily about stability:
- Near-ATM options tend to have more reliable quoted IVs than deep OTM options.
- We evaluate ladder probabilities for strikes near spot (by default), so an ATM-ish vol is a reasonable single-parameter summary.

After filtering to near-ATM options, the implementation:

- Groups the remaining option rows by expiry.
- Selects the *nearest* expiry with at least 4 usable IV samples (otherwise it falls back to the nearest expiry available).
- Computes $\sigma_{\text{implied}}$ as the **median** of the `mark_iv` values for that expiry (restricted to strikes in $[\text{lo},\text{hi}]$).

These choices are meant to be stable and explainable:
- **Strike band (`iv_band_pct`)** controls how “ATM-like” the sample set is. A narrower band reduces smile/skew contamination but may yield fewer quotes.
- **Minimum samples (4)** is a small safeguard against using a single stale print.
- **Median aggregation** reduces sensitivity to outliers among the retained quotes.

Deribit’s `mark_iv` normalization is in `kalshi_edge/market_state.py::normalize_mark_iv`:

$$
\texttt{mark\_iv\_decimal} = \frac{v}{100}
$$

### Realized Volatility: Coinbase

Realized vol is computed in `kalshi_edge/market_state.py::coinbase_realized_vol_1h`:

- Pull 1-minute candles and take the last $N$ closes.
- Compute 1-minute log returns:

$$
r_i = \ln\!\left(\frac{C_i}{C_{i-1}}\right)
$$

- Compute population standard deviation $\sigma_{\text{1m}} = \operatorname{pstdev}(\{r_i\})$.
- Annualize:

$$
\sigma_{\text{realized}} = \sigma_{\text{1m}}\sqrt{\texttt{MINUTES\_PER\_YEAR}}
$$

Realized volatility here is a deliberately **local, short-window** estimate (roughly “last hour”). The intended use is not to estimate long-run volatility, but to capture regime changes (e.g. a sudden spike) that may not yet be reflected in a single implied-vol snapshot.

### Blend Rule

The blend rule is in `kalshi_edge/market_state.py::blend_vol`. Let:

$$
\rho = \frac{\sigma_{\text{realized}}}{\sigma_{\text{implied}}}
$$

Then:

$$
\sigma_{\text{blend}} =
\begin{cases}
0.5\,\sigma_{\text{implied}} + 0.5\,\sigma_{\text{realized}} & \text{if } \rho > 1.5 \\
0.7\,\sigma_{\text{implied}} + 0.3\,\sigma_{\text{realized}} & \text{otherwise}
\end{cases}
$$

If $\sigma_{\text{implied}} \le 0$, code falls back to $\sigma_{\text{blend}}=\sigma_{\text{realized}}$.

### Why Blend Implied and Realized Volatility

We blend implied and realized volatility because each input captures different information and each has failure modes:

- **Implied volatility** is forward-looking in the sense that it is inferred from option prices. It tends to incorporate market expectations (and risk premia) about future variance over the option’s horizon. In practice, it can also be distorted by:
  - supply/demand imbalances in options,
  - volatility risk premia,
  - microstructure noise in individual quotes (especially away from the most liquid expiries/strikes).

- **Realized volatility** is purely backward-looking: it measures the variability of observed returns over a recent window. It can respond quickly to new regimes, but it is:
  - noisy over short windows,
  - sensitive to window choice,
  - not a direct forecast of future variance.

Using only one is therefore brittle:
- implied-only can under-react to sudden realized regime shifts (or reflect option-market distortions),
- realized-only can overfit recent noise and ignore forward-looking information embedded in options.

The blend rule is a simple robustness heuristic: when realized volatility is much larger than implied volatility (large $\rho$), the code increases weight on realized volatility (50/50). Otherwise it defaults to weighting implied volatility more heavily (70/30). This reflects the intuition that implied vol is usually a better “baseline” forecast, but realized vol is informative when it strongly disagrees.

### Practical Caveats

The two volatility inputs are not measured on identical horizons:
- the implied-vol estimate is taken from the nearest option expiry that provides enough near-ATM samples (often days),
- the realized-vol estimate is computed from a short recent window (about one hour),
- the Kalshi horizon $t$ can be much shorter than either.

The implementation treats both inputs as noisy proxies for an “instantaneous” volatility level and uses the blend for stability rather than theoretical consistency. If you want a horizon-matched model, you would typically use term structure (implied variance by maturity) and/or a realized-vol estimator aligned to the same horizon as $t$.

## Model Limitations

The lognormal baseline is intentionally simple. In practice, BTC returns can exhibit jumps, heavy tails, volatility clustering, and volatility skew/smile. As a result, the Normal-tail probability $1-\Phi(z)$ can be systematically miscalibrated, especially in stressed regimes or when the relevant horizon is very short. We treat this model as a fast, auditable baseline; we rely on conservative execution assumptions and diagnostics elsewhere in the pipeline to reduce the risk of over-interpreting small probability differences.

## Derived One-Sigma Move

This quantity is a scale/intuition diagnostic: it summarizes the model-implied “typical” move over the remaining time horizon given $\sigma_{\text{blend}}$. We display it in the CLI summary, but it is not directly used in the $p_{\text{model}}$ or EV calculations.

Computed in `kalshi_edge/math_models.py::expected_one_sigma_move_pct`:

$$
\texttt{one\_sigma\_move\_pct} = \sigma_{\text{blend}}\sqrt{t}\cdot 100
$$

Units:
- `one_sigma_move_pct` is in **percent** (%).
