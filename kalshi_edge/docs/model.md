# Probability model (implementation-traced)

This doc only describes formulas and rules that are directly traceable to code in this repo.

## Units and notation

- $S_0$: current BTC spot price in USD (`kalshi_edge/market_state.py::deribit_index_price`)
- $K$: ladder strike in USD (`kalshi_edge/kalshi_api.py::market_strike_from_floor`)
- `minutes_left`: time-to-close in **minutes** (`kalshi_edge/kalshi_api.py::above_markets_from_event`)
- $t$: time-to-close in **years**
- $\sigma$: annualized volatility in **decimal** units (e.g. $0.85$ means $85\%$ annualized)

The constant `MINUTES_PER_YEAR` is defined in `kalshi_edge/constants.py`.

## Win probability: lognormal baseline

Implemented in `kalshi_edge/math_models.py::lognormal_prob_above`.

The modeled probability is:

$$
p_{\text{model}} = \mathbb{P}(S_T \ge K)
$$

The code assumes:

$$
\ln\!\left(\frac{S_T}{S_0}\right)\sim \mathcal{N}(0,\sigma^2 t)
$$

with:

$$
t = \frac{\texttt{minutes\_left}}{\texttt{MINUTES\_PER\_YEAR}}
$$

Define:

$$
z = \frac{\ln(K/S_0)}{\sigma\sqrt{t}}
$$

Then the implemented closed form is:

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

## Volatility inputs and “sigma_blend”

All volatility sourcing and blending is implemented in `kalshi_edge/market_state.py::build_market_state`.

### Implied volatility (Deribit)

Implied vol comes from `kalshi_edge/market_state.py::deribit_atm_implied_vol`:

- Pull option summaries from Deribit.
- Filter option strikes within a band around spot:

$$
\text{lo} = S_0\left(1 - \frac{\texttt{iv\_band\_pct}}{100}\right)
\qquad
\text{hi} = S_0\left(1 + \frac{\texttt{iv\_band\_pct}}{100}\right)
$$

- Group by expiry and select the nearest expiry with at least 4 IV samples; otherwise fall back to the nearest expiry available.
- Use the **median** IV within the chosen expiry.

Deribit’s `mark_iv` normalization is in `kalshi_edge/market_state.py::normalize_mark_iv`:

$$
\texttt{mark\_iv\_decimal} =
\begin{cases}
\frac{v}{100} & \text{if } v > 5\\
v & \text{otherwise}
\end{cases}
$$

### Realized volatility (Coinbase)

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

### Blend rule

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

## Derived “one sigma move” (%)

Computed in `kalshi_edge/math_models.py::expected_one_sigma_move_pct`:

$$
\texttt{one\_sigma\_move\_pct} = \sigma_{\text{blend}}\sqrt{t}\cdot 100
$$

Units:
- `one_sigma_move_pct` is in **percent** (%).

## TODOs (intentionally not guessed)

- **TODO**: Confirm the exact Kalshi settlement reference and whether “ABOVE” should be treated as $S_T > K$ vs $S_T \ge K$. The current implementation uses $\ge$ in `lognormal_prob_above` and should be kept consistent with these docs unless code changes.
