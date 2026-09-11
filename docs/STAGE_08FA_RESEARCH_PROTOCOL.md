# Stage 8F-A fixed research protocol / v1

Status: methods frozen BEFORE the new August labels/cost sensitivities are calculated.
Parent: `a6c68008327f8aac2fd888572e33504ff20cd019`.
Branch: `phase-08fa-signal-cost-research`. Scope: READ_ONLY_RESEARCH / NOT_ADMISSION.
This is a prospective fixation of **this analysis on already examined development
data**, NOT an unseen-August strategy preregistration. No trading authorization.

## 1. Questions, hypotheses and one primary estimand

A: does the frozen exact-reclaim signal contain directional/timing information,
relative to same-direction background times? Null descriptive hypothesis: its
15-minute direction-adjusted return has no positive incremental mean after the
fixed past-information stratification below. A near structural target alone does
not answer this question.

**Only primary metric:** candidate-weighted, common-support stratified difference
of **15-minute signed price return in bps**, signal minus same-direction background.
For stratum c, let n_c be the number of eligible signals, S_c their mean signed
return, B_c the background mean. Delta=sum(n_c*(S_c-B_c))/sum(n_c).
The pooled LONG/SHORT metric is primary; direction-specific values are secondary.
No re-labelling the best auxiliary window as primary and no independent-trade test.

B: what declared transaction-cost conditions are necessary for the original
static structural target plan to meet its original net-RR floor? This is a
separate feasibility question, not a test of actual executable profitability.
Direction information does not establish net profitability.

## 2. Frozen population, time and provenance

* SOLUSDT only. August `[2026-08-01T00:00:00Z, 2026-09-01T00:00:00Z)` plus the
  original preceding 300-second warmup. No newly downloaded/held-out month.
* All **37,141** frozen 8D candidate IDs, directions, times and plans. No
  post-outcome filtering/re-generation, including stale/rejected candidates.
* Dataset digest: `2bd4ab5b4cdca36938dbb32aa01214d8cf5cfd236f153734b25f400d4e7fb098`.
  Existing seekable index: `e815e3c6bea4310de1f564be881740c6ebf077dbe8c644d60681b43c77354fc7`.
* Frozen source run: `4814773e5b96237582512da461358ae837100362d4ba16d7aa7906e725bdf8bc`;
  bundle: `9810bbb0f0576924df7c1d8d9b8a06f2e7e787a206424d604aff6bc2925a3897`.
  Source experiment code: `b488b0e282ef49cfbcceeb4bf84ca5aeb2d7e806`.
* Reuse 8E same-quantity decomposition, `diagnostic-runs/8e-full-v1/cost-rows.jsonl`,
  checked against its published artifact hash and source candidate identity.
  Do not rerun 98.32% attribution and call it a new finding.
* Every result: `DEVELOPMENT / ALREADY_EXAMINED`. Weekly groups are UTC days
  1-7, 8-14, 15-21, 22-28, 29-31, descriptive only, not independent holdouts.
* Protocol commit, actual code tree digest, source/index/config/8E artifact hashes,
  environment, fixed seed **808061** and actual versions recorded per new run.

## 3. Labels: fixed definitions and missingness

Primary horizon **900 seconds**. Auxiliary horizons **60, 300, 3600 seconds**.
Times are integer UTC milliseconds. Price is the last aggregate trade visible at
the observation instant; availability=event time + frozen **250ms** delay. Never
use a future event for a past sample. At equal availability order by aggregate ID.
Signals use the original candidate's last visible event identity/price/time, and
the independent price scan must agree. These are observation prices, not fills.

At t and deadline t+h, valid endpoint age must be strictly less than **15,000ms**
from actual event time. Future path is `(t,t+h]` in availability time, excluding
all events available at or after month end. Deadline >= month end is CENSORED;
never shorten a horizon or fetch September to fill it. Any observation gap of
15,000ms or greater inside the path marks INSUFFICIENT_COVERAGE; no interpolation.
Missing origin, missing endpoint, gap and censor flags stay separate, with per-
window eligible denominators. Record extrema on the observed portion even when
incomplete, but exclude incomplete labels from comparison statistics.

For sign d=+1 LONG/-1 SHORT and observed P0:
return_distance=d*(Pend-P0); return_bps=10000*return_distance/P0.
MFE=max(0,max d*(P-P0)); MAE=max(0,max -d*(P-P0)), each USDT/SOL and bps.
These are path excursions, never trade profits or assumed exit prices.
Use lossless integer price units (10^-8 USDT/SOL) while indexing, then Decimal
for labels/cost money. Unsupported finer prices are rejected, not rounded.

First original price invalidation uses the original lte/gte stop condition, not
a moved/loosened stop. Record triggering event's event time AND availability time.
For each horizon record first invalidation (if within it), MFE strictly BEFORE
that event, and whether later favorable motion occurred after invalidation. Missing
gaps mean only first OBSERVED invalidation is known, not proof of first real touch.
Background samples have no invented original stop: invalidation is NOT_APPLICABLE.
Candidate features and future labels are separate files and digests.

## 4. Background, common support and dependence

Background times are every UTC **900 seconds**, starting at August 1 midnight,
each evaluated in both directions. No exclusion for proximity to signals and no
choice using future return. Same prices, windows, gap/censor rules as signals.

Past volatility feature is `(max-min)/last*10000` over the preceding **300 seconds**
of available SOL trades. Require full lookback coverage and fresh current price.
Fixed bps bands: `[0,10), [10,25), [25,50), [50,100), [100,infinity)`.
Joint primary strata: **direction × UTC day × UTC hour × past-volatility band**.
Strata without eligible background remain UNMATCHED (no neighboring-cell fallback).
Show raw signal/background differences, stratified differences, common-support
fraction, by-side/day/hour/volatility/week descriptions and every window's counts.

Time-block uncertainty: paired resampling of the **31 UTC origin-day blocks**,
with replacement, **2,000** repetitions using Python Random seed 808061.
Each block keeps all signals, overlapping windows, backgrounds and matching
weights together. Percentile endpoints use sorted floor((N-1)*p), p=.025/.975.
The block bootstrap is an approximation with only 31 days; cross-midnight labels
and serial dependence across days remain limitations. No iid t-test or treating
37,141 labels as independent trades. Record contributing blocks, not just rows.
As a fixed dependence sensitivity, separately report the earliest candidate
(tie candidate ID) per direction per UTC 15-minute bin; do not replace primary.

Positive-direction research clue requires primary lower 95% block bound >0,
at least 20 contributing days, >=50% common-support coverage, and positive point
differences in at least 3 of the 5 fixed weeks. Otherwise state insufficient
evidence; do not claim signal validity. This is not trade approval, and even a
positive result on examined development data needs independent future validation.
Auxiliary windows/directions are descriptive, not extra discoveries/significance
claims; no family of optimized filters or multiple-comparison fishing.

## 5. Cost boundary: fixed quantity, distinct reward/loss legs

Keep each candidate's **same diagnostic quantity from 8E**, original structural
entry/stop/50-50 targets, original effective RR requirement, fee rates and frozen
funding amount at that quantity. Where 8E uses a minimum diagnostic quantity
because quantification never completed, retain that label and never call it an
approved quantity. Original rejection reasons/UNRESOLVED remain historical.

Exactly **six** counterfactual rows per candidate, no search:

1. FEES_ONLY: declared entry/exit fee .0005 (or exact original declared rate),
   original reference prices, zero spread/slippage/funding; no fill/tick claim.
2-6. Per-leg slippage **0,2,5,10,20 bps**, same declared 2bps full spread, same
   adverse BUY-ceiling/SELL-floor tick mapping and conservative combined-bps
   projection as 8D, same declared fees and **unchanged original q-materialized
   funding budget**. Do not recalculate a more favorable funding allowance when
   changing slippage. Zero slippage is an optimistic bound, not verified execution.

Use original `calculate_rr` on separate hypothetical cost inputs, never overwrite
the original RR. No ExecutionModel override, admission, scoring or order call.
Use net target G = gross target minus target-leg fees/price friction/funding;
net initial risk L = gross stop distance plus STOP-leg fees/friction/funding.
Test **G>=k*L**, L>0, at the same full quantity. Report both legs, not one common C.
10bps must reconcile to existing 8E decomposition at that quantity. If legacy
float inputs cannot losslessly represent money, mark unsupported rather than
silently round. No quantity resize, target/stop search or altered floor.

Report counts: fees already exhaust reward; positive fee-only reward below RR;
current-model failure; best optimistic grid failure; lower-cost-only necessary
condition; and static necessary-condition passes (not executable trades).
Cost evidence insufficiency is an orthogonal flag for ALL plans with unverified
execution assumptions, not a claim eliminated by an optimistic grid pass.
Report passing/failing grid points; where adjacent points straddle the threshold
report only the bracket, inclusive/strict endpoints, not a smooth interpolated
threshold. If non-monotone, list discrete outcomes without claiming one boundary.

Cost evidence table must distinguish declared/account-verified fees, assumed/book-
verified spread, assumed/own-fill-verified impact, ex-ante/settled funding and
assumed/historical exchange precision rules. No new market/account data access.

## 6. Deterministic representatives and bounded workload

Fixed examples: smallest candidate ID per side for each label status, each cost
boundary class, and invalidated-then-favorable flag; also nearest observed median
15m signed return per side (tie ID). No hand selection of spectacular paths.
One frozen full-August label pass and one six-row cost pass after synthetic tests.
Indexing uses a single SOL stream scan, reusable 15s interval extrema and threshold
first-touch heaps; range queries reuse that index, not a month scan per candidate.
Candidate metadata is capped at 40,000 and index <=180,000 bins; no all-tick array.
Hash verification reads are reported separately from analysis parsing.
Record parsed records/bytes, hash bytes, wall time, peak RSS and coverage.

Stop on schema/hash/identity/visibility inconsistency or unexplained arithmetic
failure. Preserve failed run directories and code versions. Fix genuine program
errors with synthetic regressions, rerun with a new result directory/version;
do not alter windows, filters or cost grid in response to findings. Resource cap:
60 minutes per full run / 2 GiB peak RSS (report incomplete if exceeded). No minimum
profit/trade count/significance acceptance criterion, no repeated account replay.

## 7. Isolation, tests, holdout and permitted conclusions

Reuse explicit read-only source connection, validated local dataset/index and
8E artifact; never construct Store, Broker, account, reservations, execution or
schema. New outputs under `research-runs/`, never overwrite old artifacts. No .env,
network, private Bridge, realtime, Telegram, downloaders, migrations or deployment.
Future labels cannot be imported by admission/strategy/runtime automatically.

Synthetic tests precede history: future mutation leaves features unchanged;
endpoint equality/visibility/censor/gaps; LONG/SHORT; invalidation before rebound;
MFE not PnL; signal/background parity; clustered bootstrap; cost-leg conservation;
fixed q/funding/fees; tick discontinuity; immutable source and no execution imports.
Run current project regressions and report actual results, not past counts.

Future holdout: PENDING. After final strategy, costs and selection criteria freeze,
register a start UTC date (separately confirmed BEFORE reading it) and evaluate a
continuous **30-day** `[start,start+30days)` block with any warmup explicitly marked.
Any previously inspected interval stays seen. No hidden acquisition promise.
At most one proposed next strategy research version, based on A and B separately;
none implemented. No timestamp refresh to disguise old structure evidence.
Report candidate/rejection counts; trades/PnL/equity drawdown **NOT_SIMULATED**, not
zero-profit/safe claims. Fixed-window/MFE labels and static counterfactual cash
are not executed returns. Finish and pause before 8F-B.
