"""Fixed common-support estimand and paired UTC-day block uncertainty.

Rows are dependent labels, NOT independent trades. No p-value or optimization.
"""
from collections import defaultdict, Counter
from decimal import Decimal as D, Context, localcontext
from random import Random

DAY = 86_400_000
SEED = 808061
REPLICATES = 2000


def quantiles(values):
    v = sorted(values)
    return {name: v[((len(v)-1)*n)//d] if v else None for name,n,d in (
        ('min',0,1),('p10',1,10),('p25',1,4),('p50',1,2),('p75',3,4),('p90',9,10),('max',1,1))}


def mean(values):
    return sum(values, D(0))/len(values) if values else None


def block_interval(day_sums, day_counts, *, seed=SEED, replicates=REPLICATES):
    if len(day_sums) != len(day_counts) or not day_sums or replicates <= 0:
        raise ValueError('INVALID_PAIRED_BLOCKS')
    rng = Random(seed); boot = []; skipped = 0; n = len(day_sums)
    for _ in range(replicates):
        picks = [rng.randrange(n) for _ in range(n)]
        count = sum(day_counts[i] for i in picks)
        if not count:
            skipped += 1
            continue
        boot.append(sum((day_sums[i] for i in picks), D(0))/count)
    boot.sort()
    return dict(method='PAIRED_UTC_ORIGIN_DAY_BLOCK_PERCENTILE', block_seconds=86400,
        available_blocks=n, contributing_blocks=sum(x>0 for x in day_counts),
        repetitions=replicates, usable_repetitions=len(boot), empty_repetitions=skipped, seed=seed,
        lower=boot[((len(boot)-1)*25)//1000] if boot else None,
        upper=boot[((len(boot)-1)*975)//1000] if boot else None,
        iid_samples=False, limitations='Only finite origin days; cross-midnight and inter-day dependence remain')


def compact(label):
    return dict(kind=label['kind'], side=label['side'], at=label['signal_at_ms'],
        id=label['candidate_id'], horizon=label['horizon_seconds'], eligible=label['eligible'],
        flags=label['flags'], band=label['volatility_band'],
        r=label['observed_return']['bps'] if label['observed_return'] else None,
        distance=label['observed_return']['usdt_per_sol'] if label['observed_return'] else None,
        mfe=label['mfe']['bps'] if label['mfe'] else None, mae=label['mae']['bps'] if label['mae'] else None,
        before=label['mfe_before_invalidation']['bps'] if label['mfe_before_invalidation'] else None,
        invalid=label['first_observed_invalidation'] is not None,
        later=label['later_favorable_after_invalidation'])


def compare(rows, start, end, *, bootstrap=True):
    with localcontext(Context(prec=40)):
        signals = [r for r in rows if r['kind']=='SIGNAL' and r['eligible']]
        backgrounds = [r for r in rows if r['kind']=='BACKGROUND' and r['eligible']]
        days = (end-start)//DAY
        cells = defaultdict(list)
        def cell(r):
            return (r['side'], (r['at']-start)//DAY, (r['at']//3_600_000)%24, r['band'])
        for r in backgrounds:
            if r['band'] is not None: cells[cell(r)].append(r['r'])
        centers = {k: mean(v) for k,v in cells.items()}
        matched = []; totals = [D(0)]*days; counts = [0]*days
        for r in signals:
            key = cell(r)
            if r['band'] is not None and key in centers:
                delta = r['r']-centers[key]; day = key[1]
                totals[day] += delta; counts[day] += 1; matched.append(delta)
        raw_s, raw_b = mean([r['r'] for r in signals]), mean([r['r'] for r in backgrounds])
        interval = block_interval(totals,counts) if bootstrap else None
        weekly = []
        for a in range(0,days,7):
            n = sum(counts[a:a+7])
            weekly.append(dict(first_day=a+1,last_day=min(days,a+7),n=n,
                difference_bps=sum(totals[a:a+7],D(0))/n if n else None))
        coverage = D(len(matched))/len(signals) if signals else D(0)
        clue = bool(interval and interval['lower'] is not None and interval['lower']>0 and
            interval['contributing_blocks']>=20 and coverage>=D('.5') and
            sum(w['difference_bps'] is not None and w['difference_bps']>0 for w in weekly)>=3)
        return dict(signal_count=len(signals),background_count=len(backgrounds),
            raw_signal_mean_bps=raw_s, raw_background_mean_bps=raw_b,
            raw_difference_bps=raw_s-raw_b if raw_s is not None and raw_b is not None else None,
            stratified_difference_bps=mean(matched), matched_signals=len(matched),
            unmatched_signals=len(signals)-len(matched),common_support_fraction=coverage,
            confidence_interval=interval,weekly=weekly,
            conclusion='DEVELOPMENT_DIRECTION_CLUE_ONLY' if clue else 'INSUFFICIENT_DIRECTION_EVIDENCE',
            execution_authority='NONE')


def describe_groups(rows, start):
    result = {}
    for kind in ('SIGNAL','BACKGROUND'):
        selected = [r for r in rows if r['kind']==kind]
        valid = [r for r in selected if r['eligible']]
        groups = {}
        for key,func in (
            ('utc_day',lambda r:(r['at']-start)//DAY+1),
            ('utc_hour',lambda r:(r['at']//3_600_000)%24),
            ('past_volatility_band',lambda r:r['band']),
            ('week',lambda r:((r['at']-start)//DAY)//7+1)):
            items = defaultdict(list)
            for r in valid: items[str(func(r))].append(r['r'])
            groups[key] = {k:dict(n=len(v),mean_bps=mean(v)) for k,v in sorted(items.items())}
        result[kind] = dict(total=len(selected),eligible=len(valid),
            exclusion_flags=dict(Counter(f for r in selected for f in r['flags'])),
            favorable_fraction=D(sum(r['r']>0 for r in valid))/len(valid) if valid else None,
            invalidated=sum(r['invalid'] for r in valid),invalidated_then_more_favorable=sum(r['later'] for r in valid),
            quantiles={k:quantiles([r[k] for r in valid if r[k] is not None]) for k in ('r','distance','mfe','mae','before')},
            groups=groups)
    return result


def summarize(rows, start, end):
    result = {}
    for horizon in (60,300,900,3600):
        all_h = [r for r in rows if r['horizon']==horizon]
        out = {}
        for side in ('POOLED','LONG','SHORT'):
            current = all_h if side=='POOLED' else [r for r in all_h if r['side']==side]
            earliest = {}
            for r in current:
                if r['kind']=='SIGNAL':
                    key=(r['side'],r['at']//900_000)
                    if key not in earliest or (r['at'],r['id'])<(earliest[key]['at'],earliest[key]['id']):
                        earliest[key]=r
            reduced = list(earliest.values())+[r for r in current if r['kind']=='BACKGROUND']
            out[side] = dict(comparison=compare(current,start,end),descriptions=describe_groups(current,start),
                fixed_earliest_per_15m=compare(reduced,start,end,bootstrap=False))
        result[str(horizon)]=dict(primary=horizon==900,results=out)
    return dict(version='direction-background-study/v1',primary_horizon_seconds=900,windows=result,
        metric='candidate_weighted_joint_stratum_mean_difference_bps',data_use='DEVELOPMENT / ALREADY_EXAMINED',
        independent_trade_count=None,simulated_profit=None,equity_drawdown=None,holdout='PENDING',
        no_strategy_validation=True)
