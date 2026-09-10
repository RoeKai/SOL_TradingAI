# 8D contract (implementation specification, before code)

Baseline: `efd9acc207519240af7d737a5faf9007bc7c01fc`.
Only price interpretation and quantity-dependent ex-ante costs change. The old
provider, static calculator, scoring rubric, thresholds, exit reducer, ledger,
historical funding facts, and archived experiments retain their semantics.

## Prices: execution-price-layers/v1

The original exact reclaimed price P is **structure**, not a promised fill.
For direction d (+1 LONG / -1 SHORT), total spread b and slippage s in bps:

* entry counterquote Q = P_market * (1 + d*b/20000);
* entry fill M = adverse_tick(Q * (1 + d*s/10000));
* an exit from market price X uses Qx = X*(1-d*b/20000), then
  adverse_tick(Qx*(1-d*s/10000));
* fees are fee_rate * actual modeled filled notional, once per leg;
* funding is a separate signed settlement cash flow or ex-ante debit budget.

Each price records input event/time, provenance, model, rounding, and digest.
At creation the quote must be from the same visible snapshot as the exact
signal. Adverse counterquote drift permitted before submit/accept is **0 bps**;
better quotes still require full structure/time/cost/risk revalidation. The old
50 bps max_entry_deviation is NOT an additional chase allowance. No future
price or future funding is read to issue a plan.

Arithmetic examples (per SOL, P=100, b=2, s=10, tick=.01, fee=.0005):

| Side | Counterquote | Before tick | Modeled fill | Entry fee |
|---|---:|---:|---:|---:|
| LONG / BUY | 100.0100 | 100.1100100 | 100.12 | .050060 |
| SHORT / SELL | 99.9900 | 99.8900100 | 99.89 | .049945 |

The Stage 3 RR input stays on the structure price, with a separately versioned
combined adverse bps allowance covering **exactly** spread + slippage + tick.
It is a conservative static approximation, not a second subtraction of spread.
Exit allowance is the worst supported leg's adverse relative fill deviation.
Original evidence/targets/stop/50-50 fractions are unchanged; derived costs and
results have independent IDs. Exact path cash flows use the accepted reducer
and actual rounding, with the approximation difference reported. Missing or
unrepresentable mappings are UNSUPPORTED, not rounded into permission.

## Costs: quantity-funding-budget/v1

F(q) = F_fixed + q * P_budget * .001 * 2.
F_fixed=0 is explicit: the declared synthetic fee schedule has no per-order
fixed charge; no real fee is inferred. Old `fixed-ex-ante-funding-budget/v1`
retains its entire 1 USDT cost in the A/B comparisons.

P_budget is the maximum of already-visible reference/counterquote/modeled
entry, original stop, ALL valid visible profit-side swing prices, and modeled
entry/exit prices at those locations. This includes the short loss side and
the conditional runner path; it is a bounded assumption, NOT a future maximum.
Funding credits never increase size. Actual debit exceeding frozen budget is
booked unchanged and latches a new-entry pause; existing protection continues.

## Quantification: quantity-consistent-admission/v1

Reuse original account, evidence, cost completeness, market and score checks.
Only the old market-entry interval comparison gets explicit price-layer
semantics. Never filter an old REJECT's reason list to manufacture approval.

Search the exchange lattice within all hard caps using deterministic grade
branch upper bounds and round-robin descending neighbors (32 unique sizes).
Analytic upper bounds only prune
conditions proved monotonic (linear static cash flow, nonnegative fixed cost).
Never binary-search the whole predicate: TP rounding and grades are discrete.
Every candidate rematerializes F(q), Stage 3 RR, original Scorecard, conditional
S0-S3 and all tier/account caps at that same q. Exhaustion is UNRESOLVED unless
the entire domain was checked or a recorded bound proves it infeasible.

G(q)=g*q-f0; L(q)=l*q+f0: RR>=k iff
(g-k*l)*q >= (1+k)*f0, for positive L. This is a test/proof bound only,
not a substitute for rounded scenario cash flow or the accepted RR calculator.

An issued 8D approval binds original signal, price layers, cost function,
materialization, quantity, RR/card/scenarios, account/version, policies, expiry.
It is not a Stage 5 AdmissionDecision or a reusable JSON bearer permission.
Instance storage + lock-time recomputation + atomic reservation/consumption
authorize only this offline broker. Legacy grants and entry APIs stay intact.

## Storage, evidence and experiments

New explicit schema 4 and `quantified-runs/`, no migration or initial-balance
reset. Reuse 8C transaction, broker, exit, reconciliation and model-invalid
latching with separate exact-schema composition adapters; no second PnL
ledger/state machine. The old schema guards and runtime classes stay unchanged;
thin copied scheduler methods have AST-equivalence regression assertions.
8C full-policy valuation remains UNSUPPORTED; S3 remains conditional, not
expectation. Confirmed off-model fills stay in the ledger, halt unfilled entry,
retain frozen R and establish protection. Approvals are never rewritten.

A/B/C/D attribution uses the SAME historical candidates and frozen readonly
account snapshots, independently of the D continuous account. Dates and all
baseline/stress parameters stay frozen. First-hour engineering and synthetic
active-position failures precede full-month attempts. August is development
replay, not out-of-sample validation. No network, realtime, accounts or live.
