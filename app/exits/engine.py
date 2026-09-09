"""Pure event reducer with durable-intent output. Never sends or resends orders."""

import hashlib
import json
from decimal import Context, Decimal, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext

from pydantic import TypeAdapter

from .models import (Record, EntryFill, EntrySealed, MarketEvent, ActionReceipt, ExitFill,
    ProtectionLost, RecoveryRequired, Event, ExitAction, ExitContractError, ExitResult, ExitSeed, ExitState, FillFact, StopCoverage)
from .policy import ExitPolicy
from .runner import break_even_price, inward_tick, propose_runner, trend_invalid

D=Decimal
ZERO=D(0)
EVENTS=TypeAdapter(Event)
EXIT_KINDS=('TP1','TP2','TP_COMBINED','CLOSE_ALL')
STOP_KINDS=('ARM_STOP','MOVE_STOP')
BUSY=('INTENT','ACCEPTED','UNKNOWN','SETTLING')


def digest(record: Record):
    return hashlib.sha256(json.dumps(record.model_dump(mode='json'),sort_keys=True,
        separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()


def _copy(state,**values):
    return state.model_copy(update=values)


def _fault(state,code):
    return _copy(state,phase='PROTECTION_REQUIRED',faults=tuple(dict.fromkeys((*state.faults,code))))


def _action(state,action_id):
    return next((a for a in state.actions if a.action_id==action_id),None)


def _set_action(state,action):
    done=state.completed_action_ids
    if action.status in ('FILLED','CANCELED','REJECTED') and action.action_id not in done:
        done=(*done,action.action_id)
    return _copy(state,actions=tuple(action if a.action_id==action.action_id else a for a in state.actions),
                 completed_action_ids=done)


def _emit(state,kind,reason,*,quantity=ZERO,stop_price=None,target=None,replaces=None,exact=False,protection_mode='fixed_quantity'):
    seq=len(state.actions)+1
    action=ExitAction(action_id='0'*64,position_id=state.position_id,sequence=seq,kind=kind,
        reason_code=reason,quantity=quantity,stop_price=stop_price,target_action_id=target,
        replaces_action_id=replaces,close_exact_remainder=exact,side='SELL' if state.side=='LONG' else 'BUY',
        position_quantity_version=state.position_quantity_version,protection_mode=protection_mode)
    key=hashlib.sha256((state.seed_digest+state.policy_digest+digest(action)).encode()).hexdigest()
    action=action.model_copy(update={'action_id':key})
    return _copy(state,actions=(*state.actions,action))


def _control_failures(attempts):
    failures=0
    for old in reversed(attempts):
        if old.target_confirmed: break
        failures+=old.status in ('REJECTED','CANCELED')
    return failures


def _control_once(state,policy,kind,target,reason):
    order=_action(state,target)
    if kind=='CANCEL' and order is not None and order.lifecycle_terminal:
        # Terminal orders cannot be canceled again, but may still owe fills.
        if not order.settlement_complete:
            return _control_once(state,policy,'RECONCILE',target,'TERMINAL_FILL_DETAILS_REQUIRED')
        return state
    if kind=='CANCEL' and target==state.entry_action_id and state.entry_sealed: return state
    attempts=[a for a in state.actions if a.kind==kind and a.target_action_id==target]
    if attempts:
        last=attempts[-1]
        if last.status=='UNKNOWN':
            if kind=='CANCEL':
                return _control_once(state,policy,'RECONCILE',target,'UNKNOWN_CANCEL_QUERY_ORIGINAL_TARGET')
            return _fault(state,'RECONCILE_RESULT_UNKNOWN_REQUIRES_REVIEW')
        if last.status in ('INTENT','ACCEPTED','SETTLING'): return state
        if last.status=='FILLED' and not last.target_confirmed:
            return _fault(state,'CONTROL_RESULT_WITHOUT_TARGET_CONFIRMATION')
        # Bound attempts since the last successful authoritative target check.
        if _control_failures(attempts)>=policy.max_control_attempts:
            return _fault(state,kind+'_CONTROL_ATTEMPTS_EXHAUSTED')
    return _emit(state,kind,reason,target=target)


def _target_settled(action):
    return action.status in ('ACCEPTED','FILLED','CANCELED','REJECTED') and action.settlement_complete


def _resolve_controls(state,target,status):
    """Only an authoritative TARGET fact resolves control work, not its ACK."""
    if status not in ('ACCEPTED','FILLED','CANCELED','REJECTED'): return state
    order=_action(state,target)
    if order is not None and not _target_settled(order): return state
    for action in state.actions:
        if (action.target_action_id==target and action.kind in ('CANCEL','RECONCILE')
            and action.status in (*BUSY,'FILLED') and not action.target_confirmed
            and (action.kind=='RECONCILE' or status!='ACCEPTED')):
            state=_set_action(state,action.model_copy(update={'status':'FILLED','target_confirmed':True}))
    return state


def _coverage_status(state):
    action=_action(state,state.protection_action_id)
    if action is None or action.lifecycle_terminal:
        return ZERO,None,'MISSING'
    coverage=action.confirmed_coverage
    if action.status in ('UNKNOWN','SETTLING'):
        return ZERO,coverage.quantity_version if coverage else None,'UNKNOWN'
    if not action.stop_confirmed or coverage is None: return ZERO,None,'MISSING'
    capacity=(state.remaining_quantity if coverage.mode=='dynamic_position' else
              max(ZERO,coverage.quantity-action.filled_quantity))
    covered=min(state.remaining_quantity,capacity)
    status='ACTIVE' if covered>=state.remaining_quantity and covered>0 else 'PARTIAL' if covered>0 else 'MISSING'
    return covered,coverage.quantity_version,status


def _refresh_coverage(state):
    covered,version,status=_coverage_status(state)
    pending=[a for a in state.actions if a.kind in STOP_KINDS and a.status in ('INTENT','UNKNOWN','SETTLING')]
    if pending:
        status='UNKNOWN' if any(a.status in ('UNKNOWN','SETTLING') for a in pending) else 'PENDING'
    return _copy(state,protection_covered_quantity=covered,protection_coverage_version=version,protection_status=status)


def _stop_intent(state,seed,kind,reason,price):
    return _emit(state,kind,reason,quantity=state.remaining_quantity,stop_price=price,
        replaces=state.protection_action_id if kind=='MOVE_STOP' else None,
        protection_mode='dynamic_position' if seed.rules.dynamic_full_position_stop else 'fixed_quantity')


def _quote(state,policy,now):
    m=state.last_market
    if (m is None or not m.confirmed or m.observed_at>now or m.bid is None or m.ask is None
        or m.ask<m.bid or D(str(now))-D(str(m.observed_at))>policy.market_max_age_seconds):
        return None
    return m.bid if state.side=='LONG' else m.ask


def _tightens(state,price):
    return price>state.current_stop if state.side=='LONG' else price<state.current_stop


def _crossed(state,price,level):
    return price<=level if state.side=='LONG' else price>=level


def _minimum_ok(quantity,price,rules):
    return (quantity>0 and quantity<=rules.max_quantity and quantity%rules.quantity_step==0
            and (rules.reduce_only_min_quantity_exempt or quantity>=rules.min_quantity)
            and (rules.reduce_only_min_notional_exempt or price is not None and quantity*price>=rules.min_notional))


def _failures(state,kind):
    return sum(a.kind==kind and a.status in ('REJECTED','CANCELED') and a.filled_quantity==0 for a in state.actions)


def _close_all(state,seed,price,reason):
    rules=seed.rules
    q=min(state.remaining_quantity,rules.max_quantity)
    if q<state.remaining_quantity:
        q=(q/rules.quantity_step).to_integral_value(rounding=ROUND_FLOOR)*rules.quantity_step
        if q<=0: return _fault(state,'EXIT_MAX_QUANTITY_BELOW_STEP')
    if not rules.verified:
        return _fault(state,'EXIT_RULES_UNVERIFIED')
    # Exact remainder is an explicit adapter capability, never a size uplift.
    exact=not _minimum_ok(q,price,rules)
    if exact and not rules.exact_close_remainder:
        return _fault(state,'UNMANAGEABLE_REMAINDER_REQUIRES_ADAPTER')
    state=_emit(state,'CLOSE_ALL',reason,quantity=q,exact=exact)
    return _copy(state,phase='STOP_PENDING')


def _drive(state,seed,policy,now,reasons):
    state=_refresh_coverage(state)
    if state.faults:
        reasons.extend(state.faults)
        return _control_once(state,policy,'RECONCILE',state.position_id,'STATE_RECONCILIATION_REQUIRED')
    if state.recovery_pending_action_ids:
        for key in state.recovery_pending_action_ids:
            state=_control_once(state,policy,'RECONCILE',key,'RESTART_RECONCILE_ORIGINAL_ACTION')
        reasons.append('RECOVERY_RECONCILIATION_REQUIRED')
        return _copy(state,phase='PROTECTION_REQUIRED')
    if state.phase=='PROTECTION_REQUIRED' and not state.emergency_reason:
        state=_copy(state,phase='RUNNER' if state.tp2_complete else 'TP1_FILLED' if state.tp1_complete else 'OPEN')
    exits=[a for a in state.actions if a.kind in EXIT_KINDS and a.status in BUSY]
    pending_stops=[a for a in state.actions if a.kind in STOP_KINDS and a.status in ('INTENT','UNKNOWN','SETTLING')]
    if state.remaining_quantity==0:
        for a in (*exits,*pending_stops):
            state=_control_once(state,policy,'CANCEL',a.action_id,'FLAT_CANCEL_REMAINING_INTENT')
        if state.protection_action_id:
            state=_control_once(state,policy,'CANCEL',state.protection_action_id,'FLAT_CANCEL_PROTECTION')
        if not state.entry_sealed:
            reasons.append('ENTRY_NOT_TERMINAL_AFTER_EXIT')
            state=_control_once(state,policy,'CANCEL',state.entry_action_id,'CANCEL_UNFINISHED_OPENING_LEG')
            return _copy(state,phase='PROTECTION_REQUIRED',runner_quantity=ZERO)
        return _copy(state,phase='CLOSED',runner_quantity=ZERO)
    price=_quote(state,policy,now)
    if D(str(now))-D(str(state.opened_at))>=policy.max_holding_seconds:
        state=_copy(state,emergency_reason=state.emergency_reason or 'TIME_EXIT')
    if price is not None and _crossed(state,price,state.current_stop):
        state=_copy(state,emergency_reason=state.emergency_reason or 'STOP_CROSSED_OR_GAPPED')
    if state.last_market and trend_invalid(state,state.last_market.model_copy(update={'received_at':now}),policy):
        state=_copy(state,emergency_reason=state.emergency_reason or 'CONFIRMED_TREND_INVALIDATION')
    if state.emergency_reason:
        reasons.append(state.emergency_reason)
        for a in exits:
            if a.kind!='CLOSE_ALL':
                state=_control_once(state,policy,'CANCEL',a.action_id,'EXIT_PRIORITY_CANCEL_TP')
            if a.status in ('UNKNOWN','SETTLING'):
                state=_control_once(state,policy,'RECONCILE',a.action_id,'ORIGINAL_ACTION_RECONCILIATION')
        for a in pending_stops:
            state=_control_once(state,policy,'RECONCILE',a.action_id,'STOP_RESULT_UNCONFIRMED')
        if exits or pending_stops:
            return _copy(state,phase='PROTECTION_REQUIRED' if pending_stops or any(a.status=='UNKNOWN' for a in exits) else 'STOP_PENDING')
        if state.protection_status in ('ACTIVE','PARTIAL','UNKNOWN') and state.protection_action_id:
            state=_control_once(state,policy,'CANCEL',state.protection_action_id,'CANCEL_OR_RECONCILE_NATIVE_STOP_BEFORE_CLOSE')
            if state.protection_status=='UNKNOWN':
                state=_control_once(state,policy,'RECONCILE',state.protection_action_id,'ORIGINAL_STOP_RECONCILIATION')
            return _copy(state,phase='STOP_PENDING')
        if _failures(state,'CLOSE_ALL')>=policy.max_known_zero_fill_failures:
            return _fault(state,'EMERGENCY_EXIT_REPEATED_REJECTION')
        return _close_all(state,seed,price,state.emergency_reason)
    if exits:
        for a in exits:
            if a.status in ('UNKNOWN','SETTLING'):
                state=_control_once(state,policy,'RECONCILE',a.action_id,'ORIGINAL_ACTION_RECONCILIATION')
        phase='PROTECTION_REQUIRED' if any(a.status in ('UNKNOWN','SETTLING') for a in exits) else 'TP2_PENDING' if exits[0].kind=='TP2' else 'TP1_PENDING'
        return _copy(state,phase=phase)
    if pending_stops:
        for a in pending_stops:
            if a.status in ('UNKNOWN','SETTLING'):
                state=_control_once(state,policy,'RECONCILE',a.action_id,'ORIGINAL_STOP_RECONCILIATION')
        return _copy(state,phase='PROTECTION_REQUIRED') if any(a.status in ('UNKNOWN','SETTLING') for a in pending_stops) else state
    if state.protection_status=='PARTIAL':
        reasons.append('PROTECTION_COVERAGE_INSUFFICIENT')
        if not seed.rules.verified or not seed.rules.atomic_stop_replace:
            return _fault(state,'PROTECTION_COVERAGE_REPAIR_UNAVAILABLE')
        state=_stop_intent(state,seed,'MOVE_STOP','REPAIR_PROTECTION_COVERAGE',state.current_stop)
        return _copy(state,protection_status='PENDING',phase='PROTECTION_REQUIRED')
    if state.protection_status!='ACTIVE':
        if state.protection_status=='UNKNOWN':
            return _copy(_control_once(state,policy,'RECONCILE',state.protection_action_id,'ORIGINAL_STOP_RECONCILIATION'),phase='PROTECTION_REQUIRED')
        if not seed.rules.verified or state.current_stop%seed.rules.price_tick!=0:
            return _drive(_copy(state,emergency_reason='INITIAL_PROTECTION_NOT_ARMABLE'),seed,policy,now,reasons)
        state=_stop_intent(state,seed,'ARM_STOP','ARM_INITIAL_PROTECTION',state.current_stop)
        return _copy(state,protection_status='PENDING')
    if not state.entry_sealed:
        reasons.append('ENTRY_NOT_SEALED_NO_TP')
        return state
    # Already-filled TP1, not a price touch, is the prerequisite for break-even.
    if state.tp1_complete:
        desired=break_even_price(state,policy)
        if desired<=0:
            return _drive(_copy(state,emergency_reason='COST_COVER_STOP_INVALID'),seed,policy,now,reasons)
        desired=inward_tick(desired,seed.rules.price_tick,state.side)
        if state.tp2_complete and price is not None:
            proposal=propose_runner(state,state.last_market,policy)
            reasons.append(proposal.reason_code)
            if proposal.stop_price is not None:
                candidate=inward_tick(proposal.stop_price,seed.rules.price_tick,state.side)
                desired=max(desired,candidate) if state.side=='LONG' else min(desired,candidate)
        if _tightens(state,desired):
            if price is None:
                reasons.append('NO_FRESH_QUOTE_KEEP_CONFIRMED_STOP')
                return state
            if _crossed(state,price,desired):
                return _drive(_copy(state,emergency_reason='PROPOSED_STOP_ALREADY_CROSSED'),seed,policy,now,reasons)
            if not seed.rules.atomic_stop_replace:
                return _drive(_copy(state,emergency_reason='ATOMIC_STOP_REPLACE_UNAVAILABLE'),seed,policy,now,reasons)
            state=_stop_intent(state,seed,'MOVE_STOP','RUNNER_STOP_TIGHTEN' if state.tp2_complete else 'TP1_CONFIRMED_BREAK_EVEN',desired)
            return _copy(state,protection_status='PENDING')
    if state.tp2_complete:
        return _copy(state,phase='RUNNER',runner_quantity=state.remaining_quantity)
    if price is None:
        reasons.append('NO_FRESH_QUOTE_NO_DISCRETIONARY_TP')
        return state
    sign=1 if state.side=='LONG' else -1
    reached=sign*(price-state.frozen_r_anchor_entry)
    q1=state.tp1_planned-state.tp1_filled
    q2=state.tp2_planned-state.tp2_filled
    kind=None; q=ZERO
    if not state.tp1_complete and reached>=policy.tp1_r*state.frozen_initial_r:
        if _minimum_ok(q1,price,seed.rules): kind='TP1';q=q1
        elif reached>=policy.tp2_r*state.frozen_initial_r and _minimum_ok(q1+q2,price,seed.rules):
            kind='TP_COMBINED';q=q1+q2
        else: reasons.append('TP1_BELOW_MINIMUM_DEFER_WITHOUT_FAKE_FILL')
    elif state.tp1_complete and reached>=policy.tp2_r*state.frozen_initial_r:
        if _minimum_ok(q2,price,seed.rules): kind='TP2';q=q2
        else: reasons.append('TP2_BELOW_MINIMUM_DEFER_WITHOUT_FAKE_FILL')
    if kind:
        if _failures(state,kind)>=policy.max_known_zero_fill_failures:
            reasons.append('TP_RETRY_BUDGET_EXHAUSTED_KEEP_PROTECTION')
            return state
        residual=state.remaining_quantity-q
        if residual>0 and not _minimum_ok(residual,price,seed.rules):
            # Avoid creating an unmanageable remainder. Full close is separately named.
            return _drive(_copy(state,emergency_reason='DUST_SWEEP_FULL_REMAINDER'),seed,policy,now,reasons)
        state=_emit(state,kind,'PRICE_TRIGGER_INTENT_ONLY',quantity=q)
        return _copy(state,phase='TP1_PENDING' if kind in ('TP1','TP_COMBINED') else 'TP2_PENDING')
    return state


def _fill_fact(event,action_id,entry_basis=None,entry_basis_notional=None):
    return FillFact(fill_id=event.fill_id,action_id=action_id,quantity=event.quantity,
                    price=event.price,fee_usdt=event.fee_usdt,occurred_at=event.occurred_at,
                    entry_basis_price=entry_basis,entry_basis_notional=entry_basis_notional)


def _is_duplicate_fill(state,fact):
    old=next((f for f in state.fill_facts if f.fill_id==fact.fill_id),None)
    blank={'entry_basis_price':None,'entry_basis_notional':None}
    if old is not None and old.model_copy(update=blank)!=fact.model_copy(update=blank):
        raise ExitContractError('Fill ID reused with different immutable facts')
    return old is not None


def _apply_fill(state,event):
    action=_action(state,event.action_id)
    if action is None or action.kind not in (*EXIT_KINDS,*STOP_KINDS):
        return _fault(state,'UNRECOGNIZED_EXIT_FILL_REQUIRES_RECONCILIATION')
    fact=_fill_fact(event,event.action_id)
    if _is_duplicate_fill(state,fact): return state
    if event.quantity>state.remaining_quantity:
        return _fault(state,'CONFIRMED_FILL_EXCEEDS_REMAINING')
    filled=action.filled_quantity+event.quantity
    if action.kind in EXIT_KINDS and filled>action.quantity:
        return _fault(state,'CONFIRMED_FILL_EXCEEDS_INTENT')
    basis=state.remaining_entry_cost/state.remaining_quantity
    consumed=(state.remaining_entry_cost if event.quantity==state.remaining_quantity else
              state.remaining_entry_cost*event.quantity/state.remaining_quantity)
    cost=state.remaining_entry_cost-consumed
    remaining=state.remaining_quantity-event.quantity
    fact=_fill_fact(event,event.action_id,basis,consumed)
    sign=1 if state.side=='LONG' else -1
    exit_notional=state.exit_notional+event.price*event.quantity
    gross=sign*(exit_notional-(state.entry_notional-cost))
    fees=state.exit_fees+event.fee_usdt
    state=_copy(state,remaining_quantity=remaining,remaining_entry_cost=cost,
        remaining_average_entry=cost/remaining if remaining else None,exit_notional=exit_notional,
        position_quantity_version=state.position_quantity_version+1,
        exit_fees=fees,realized_gross_pnl=gross,realized_net_pnl=gross-state.entry_fees-fees,
        fill_facts=(*state.fill_facts,fact),last_confirmation_event=event.event_id,
        confirmed_fill_action_ids=tuple(dict.fromkeys((*state.confirmed_fill_action_ids,event.action_id))))
    p1,p2=state.tp1_filled,state.tp2_filled
    if action.kind in ('TP1','TP_COMBINED'):
        allocated=min(event.quantity,state.tp1_planned-p1);p1+=allocated
        if action.kind=='TP_COMBINED': p2+=event.quantity-allocated
    elif action.kind=='TP2': p2+=event.quantity
    tp1=state.tp1_complete or state.tp1_planned>0 and p1==state.tp1_planned
    tp2=state.tp2_complete or state.tp2_planned>0 and p2==state.tp2_planned
    milestones=state.milestones
    phase=state.phase
    if tp1 and not state.tp1_complete: milestones=(*milestones,'TP1_FILLED');phase='TP1_FILLED'
    if tp2 and not state.tp2_complete: milestones=(*milestones,'TP2_FILLED');phase='RUNNER'
    state=_copy(state,tp1_filled=p1,tp2_filled=p2,tp1_complete=tp1,tp2_complete=tp2,
        milestones=milestones,phase=phase,runner_quantity=state.remaining_quantity if tp2 else ZERO)
    status=action.status
    if action.kind in EXIT_KINDS and filled==action.quantity: status='FILLED'
    if action.kind in STOP_KINDS:
        state=_copy(state,emergency_reason='CONFIRMED_STOP_EXECUTION',protection_status='UNKNOWN')
        status='FILLED' if state.remaining_quantity==0 else 'SETTLING'
    if action.terminal_quantity is not None:
        if filled>action.terminal_quantity:
            state=_fault(state,'LATE_FILL_CONTRADICTS_TERMINAL_TOTAL')
        status=action.terminal_status if filled>=action.terminal_quantity else 'SETTLING'
    elif filled<action.known_filled_quantity:
        status='SETTLING'
    elif status=='SETTLING':
        status='ACCEPTED'
    return _set_action(state,action.model_copy(update={'filled_quantity':filled,'status':status,
        'acknowledged_quantity':max(action.known_filled_quantity,filled)}))


def _confirmed_coverage(seed,state,action,event):
    coverage=event.coverage
    if coverage is None:
        # Legacy boolean ACKs mean only the immutable fixed order quantity.
        # They NEVER grant dynamic or future-opening-fill coverage.
        if not event.covers_remaining or action.protection_mode!='fixed_quantity': return None
        coverage=StopCoverage(quantity=action.quantity,quantity_version=action.position_quantity_version,
                              evidence_id=event.event_id)
    if (coverage.mode!=action.protection_mode
        or not action.position_quantity_version<=coverage.quantity_version<=state.position_quantity_version): return None
    if coverage.mode=='fixed_quantity':
        if coverage.quantity!=action.quantity or coverage.dynamic_contract_id is not None: return None
    elif (not seed.rules.verified or not seed.rules.dynamic_full_position_stop
          or coverage.dynamic_contract_id!=seed.rules.dynamic_stop_contract_id
          or coverage.quantity_version!=state.position_quantity_version
          or coverage.quantity<state.remaining_quantity+event.cumulative_filled_quantity): return None
    return coverage


def _receipt(seed,state,event):
    action=_action(state,event.action_id)
    if action is None: return _fault(state,'UNRECOGNIZED_ACTION_RECEIPT')
    if action.kind in ('CANCEL','RECONCILE'):
        # Acknowledging cancellation is NOT confirmation that its target is canceled.
        if action.status in ('FILLED','CANCELED','REJECTED'): return state
        return _set_action(state,action.model_copy(update={'status':event.status}))
    if action.kind in EXIT_KINDS and event.cumulative_filled_quantity>action.quantity:
        return _fault(state,'RECEIPT_QUANTITY_EXCEEDS_INTENT')
    if action.terminal_status is not None:
        # The lifecycle latch survives SETTLING. Older ACKs (including UNKNOWN)
        # cannot restore a retired order or transfer the protection identity.
        if event.status in ('ACCEPTED','UNKNOWN'):
            if event.cumulative_filled_quantity>action.terminal_quantity:
                return _fault(state,'NONTERMINAL_RECEIPT_EXCEEDS_TERMINAL_TOTAL')
        elif (event.status!=action.terminal_status
              or event.cumulative_filled_quantity!=action.terminal_quantity):
            return _fault(state,'CONFLICTING_TERMINAL_RECEIPT')
        return state  # Fill details are processed independently by _apply_fill.
    if event.status=='UNKNOWN':
        if action.lifecycle_terminal: return state
        state=_set_action(state,action.model_copy(update={'status':'UNKNOWN',
            'acknowledged_quantity':max(action.known_filled_quantity,event.cumulative_filled_quantity)}))
        return _copy(state,protection_status='UNKNOWN') if action.kind in STOP_KINDS else state
    if event.status!='ACCEPTED' and event.cumulative_filled_quantity<action.filled_quantity:
        return _fault(state,'RECEIPT_BEHIND_CONFIRMED_FILLS')
    if event.status!='ACCEPTED' and event.cumulative_filled_quantity<action.known_filled_quantity:
        return _fault(state,'TERMINAL_TOTAL_BEHIND_KNOWN_CUMULATIVE')
    if event.status=='ACCEPTED' and (action.lifecycle_terminal
                                    or event.cumulative_filled_quantity<action.known_filled_quantity):
        return state  # Stale snapshot: no state regression or reconciliation release.
    known=max(action.known_filled_quantity,event.cumulative_filled_quantity)
    if action.kind in STOP_KINDS and event.status=='ACCEPTED':
        if (not event.reduce_only_verified or event.stop_price!=action.stop_price
            or action.kind=='MOVE_STOP' and (not event.old_stop_retired or event.retired_stop_cumulative_filled is None)):
            return _fault(state,'STOP_ACK_NOT_AUTHORITATIVE')
        coverage=_confirmed_coverage(seed,state,action,event)
        if coverage is None: return _fault(state,'STOP_COVERAGE_CONTRACT_INVALID')
        if action.stop_price!=state.current_stop and not _tightens(state,action.stop_price):
            return _fault(state,'STOP_WIDENING_FORBIDDEN')
        old=None
        if action.replaces_action_id:
            old=_action(state,action.replaces_action_id)
            if old is None: return _fault(state,'STOP_REPLACEMENT_IDENTITY_MISMATCH')
            if event.retired_stop_cumulative_filled<old.filled_quantity:
                return _fault(state,'RETIRED_STOP_TOTAL_BEHIND_FILLS')
            if event.retired_stop_cumulative_filled<old.known_filled_quantity:
                return _fault(state,'RETIRED_STOP_TOTAL_BEHIND_KNOWN_CUMULATIVE')
            if old.terminal_status is not None and (old.terminal_status!='CANCELED'
                    or old.terminal_quantity!=event.retired_stop_cumulative_filled):
                return _fault(state,'CONFLICTING_RETIRED_STOP_TERMINAL_RECEIPT')
        state=_set_action(state,action.model_copy(update={
            'status':'SETTLING' if known>action.filled_quantity else 'ACCEPTED',
            'acknowledged_quantity':known,'stop_confirmed':True,
            'confirmed_coverage':coverage}))
        if action.replaces_action_id:
            state=_set_action(state,old.model_copy(update={
                'status':'SETTLING' if event.retired_stop_cumulative_filled>old.filled_quantity else 'CANCELED',
                'acknowledged_quantity':max(old.known_filled_quantity,event.retired_stop_cumulative_filled),
                'terminal_status':'CANCELED','terminal_quantity':event.retired_stop_cumulative_filled}))
        return _copy(state,current_stop=action.stop_price,protection_status='ACTIVE',
                     protection_action_id=action.action_id,last_confirmation_event=event.event_id)
    if event.status=='ACCEPTED':
        if not event.reduce_only_verified: return _fault(state,'EXIT_REDUCE_ONLY_UNCONFIRMED')
        state=_set_action(state,action.model_copy(update={
            'status':'SETTLING' if known>action.filled_quantity else 'ACCEPTED',
            'acknowledged_quantity':known}))
        return _copy(state,last_confirmation_event=event.event_id)
    if event.status=='FILLED' and action.kind in EXIT_KINDS and event.cumulative_filled_quantity!=action.quantity:
        return _fault(state,'FILLED_STATUS_WITH_PARTIAL_QUANTITY')
    status='SETTLING' if event.cumulative_filled_quantity>action.filled_quantity else event.status
    state=_set_action(state,action.model_copy(update={'status':status,'terminal_status':event.status,
                                'acknowledged_quantity':known,
                                'terminal_quantity':event.cumulative_filled_quantity}))
    if action.kind in STOP_KINDS and status in ('REJECTED','CANCELED'):
        if action.action_id==state.protection_action_id:
            state=_copy(state,protection_status='MISSING',protection_action_id=None)
        elif action.kind=='MOVE_STOP':
            state=_copy(state,protection_status='ACTIVE' if state.protection_action_id else 'MISSING')
        # Failure before confirmation differs from retiring a proven replacement
        # later during emergency exit. The latter must not strand the remainder.
        if action.reason_code=='REPAIR_PROTECTION_COVERAGE' and not action.stop_confirmed:
            return _fault(state,'PROTECTION_COVERAGE_REPAIR_FAILED')
        state=_copy(state,emergency_reason=state.emergency_reason or 'PROTECTION_ORDER_FAILED_OR_CANCELED')
    return _copy(state,last_confirmation_event=event.event_id)


def _validate(seed,policy,state):
    if type(seed) is not ExitSeed or type(policy) is not ExitPolicy or type(state) is not ExitState:
        raise ExitContractError('Exact immutable exit records required')
    seed=ExitSeed.model_validate(seed.model_dump());policy=ExitPolicy.model_validate(policy.model_dump())
    state=ExitState.model_validate(state.model_dump())
    if (state.seed_digest!=digest(seed) or state.policy_digest!=digest(policy) or state.position_id!=seed.position_id
        or state.entry_action_id!=seed.first_fill.entry_action_id
        or state.original_stop!=seed.plan.original_stop or state.side!=seed.plan.side or state.symbol!=seed.plan.symbol
        or state.frozen_r_anchor_entry!=seed.first_fill.price
        or state.frozen_initial_r!=abs(seed.first_fill.price-seed.plan.original_stop)):
        raise ExitContractError('State/seed/policy or frozen initial risk mismatch')
    entries=[f for f in state.fill_facts if f.action_id==state.entry_action_id]
    exits=[f for f in state.fill_facts if f.action_id!=state.entry_action_id]
    total=sum((f.quantity for f in entries),ZERO)
    if total!=state.original_quantity or total-sum((f.quantity for f in exits),ZERO)!=state.remaining_quantity:
        raise ExitContractError('Confirmed quantity conservation failed')
    if sum((f.quantity*f.price for f in entries),ZERO)!=state.entry_notional or state.actual_average_entry!=state.entry_notional/total:
        raise ExitContractError('Actual entry average mismatch')
    # Independently replay remaining-cost allocation in confirmed ingestion order.
    # Exchange occurrence times do not retroactively re-price already booked exits.
    qty=cost=ZERO
    for fact in state.fill_facts:
        if fact.action_id==state.entry_action_id:
            qty+=fact.quantity;cost+=fact.price*fact.quantity
            if fact.entry_basis_price is not None or fact.entry_basis_notional is not None:
                raise ExitContractError('Entry fact cannot carry an exit cost allocation')
        else:
            if fact.quantity>qty: raise ExitContractError('Exit cost lacks confirmed inventory')
            basis=cost/qty
            allocated=cost if fact.quantity==qty else cost*fact.quantity/qty
            if fact.entry_basis_price!=basis or fact.entry_basis_notional!=allocated:
                raise ExitContractError('Remaining cost allocation mismatch')
            qty-=fact.quantity;cost-=allocated
    if (state.remaining_entry_cost!=cost or state.remaining_average_entry!=(cost/qty if qty else None)
        or state.position_quantity_version!=len(state.fill_facts)-1):
        raise ExitContractError('Remaining position cost/version mismatch')
    if (state.side=='LONG' and state.current_stop<state.original_stop or state.side=='SHORT' and state.current_stop>state.original_stop):
        raise ExitContractError('Protection stop widened beyond original')
    if state.tp1_filled>state.tp1_planned or state.tp2_filled>state.tp2_planned:
        raise ExitContractError('TP over-allocation')
    p1=p2=ZERO
    for f in exits:
        a=_action(state,f.action_id)
        if a is None: raise ExitContractError('Unattributed persisted exit fill')
        if a.kind=='TP1': p1+=f.quantity
        elif a.kind=='TP2': p2+=f.quantity
        elif a.kind=='TP_COMBINED':
            allocated=min(f.quantity,state.tp1_planned-p1);p1+=allocated;p2+=f.quantity-allocated
    if (p1,p2)!=(state.tp1_filled,state.tp2_filled) or (state.tp1_complete,state.tp2_complete)!=(
            state.tp1_planned>0 and p1==state.tp1_planned,state.tp2_planned>0 and p2==state.tp2_planned):
        raise ExitContractError('TP milestones do not match confirmed fill facts')
    if len({f.fill_id for f in state.fill_facts})!=len(state.fill_facts):
        raise ExitContractError('Duplicate fill identities in persisted state')
    if state.confirmed_fill_action_ids!=tuple(dict.fromkeys(f.action_id for f in exits)):
        raise ExitContractError('Executed IDs do not match confirmed fills')
    if state.opened_at!=min(f.occurred_at for f in entries):
        raise ExitContractError('Opening time does not match confirmed facts')
    if state.phase=='CLOSED' and (state.remaining_quantity!=0 or not state.entry_sealed):
        raise ExitContractError('Closed state lacks confirmed flat sealed opening leg')
    if state.runner_quantity!=(state.remaining_quantity if state.tp2_complete else ZERO):
        raise ExitContractError('Runner quantity does not match confirmed state')
    terminal={a.action_id for a in state.actions if a.status in ('FILLED','CANCELED','REJECTED')}
    if set(state.completed_action_ids)!=terminal or len(state.completed_action_ids)!=len(terminal):
        raise ExitContractError('Terminal action IDs mismatch')
    if state.entry_fees!=sum((f.fee_usdt for f in entries),ZERO) or state.exit_fees!=sum((f.fee_usdt for f in exits),ZERO):
        raise ExitContractError('Confirmed fees mismatch')
    exit_notional=sum((f.quantity*f.price for f in exits),ZERO)
    if state.exit_notional!=exit_notional:
        raise ExitContractError('Exit notional does not match confirmed cashflows')
    gross=(exit_notional-(state.entry_notional-cost))*(1 if state.side=='LONG' else -1)
    if state.realized_gross_pnl!=gross or state.realized_net_pnl!=gross-state.entry_fees-state.exit_fees:
        raise ExitContractError('Confirmed cashflow/cost PnL conservation failed')
    if state.remaining_quantity==0 and (cost!=0 or gross!=(exit_notional-state.entry_notional)*(1 if state.side=='LONG' else -1)):
        raise ExitContractError('Flat position cashflow conservation failed')
    if state.entry_sealed:
        q1=(total*policy.tp1_fraction/seed.rules.quantity_step).to_integral_value(rounding=ROUND_FLOOR)*seed.rules.quantity_step
        q2=(total*policy.tp2_fraction/seed.rules.quantity_step).to_integral_value(rounding=ROUND_FLOOR)*seed.rules.quantity_step
        if (state.tp1_planned,state.tp2_planned,state.runner_planned)!=(q1,q2,total-q1-q2):
            raise ExitContractError('Original quantity exit allocation changed')
    confirmed_stops=[state.original_stop,*(a.stop_price for a in state.actions if a.stop_confirmed)]
    if state.current_stop!=(max(confirmed_stops) if state.side=='LONG' else min(confirmed_stops)):
        raise ExitContractError('Confirmed stop history cannot be widened or invented')
    if len(dict(state.event_receipts))!=len(state.event_receipts) or len(state.event_receipts)!=state.version+1:
        raise ExitContractError('Event revision mismatch')
    for index,action in enumerate(state.actions,1):
        if action.filled_quantity!=sum((f.quantity for f in exits if f.action_id==action.action_id),ZERO):
            raise ExitContractError('Action cumulative fill does not match actual fills')
        if (action.terminal_status is None)!=(action.terminal_quantity is None):
            raise ExitContractError('Terminal lifecycle requires its cumulative quantity')
        if action.acknowledged_quantity!=action.known_filled_quantity:
            raise ExitContractError('Known cumulative execution high-water mark regressed')
        if action.terminal_status is not None:
            if (action.status not in ('SETTLING',action.terminal_status)
                or action.filled_quantity<action.terminal_quantity and action.status!='SETTLING'):
                raise ExitContractError('Terminal lifecycle cannot be restored to a live order')
            if (action.terminal_quantity<action.known_filled_quantity
                and 'LATE_FILL_CONTRADICTS_TERMINAL_TOTAL' not in state.faults):
                raise ExitContractError('Terminal cumulative execution conflicts with known facts')
        if (action.filled_quantity<action.known_filled_quantity
            and action.status not in ('SETTLING','UNKNOWN')):
            raise ExitContractError('Missing fill details cannot be treated as settled')
        if action.confirmed_coverage is not None:
            c=action.confirmed_coverage
            if (action.kind not in STOP_KINDS or not action.stop_confirmed or c.mode!=action.protection_mode
                or not action.position_quantity_version<=c.quantity_version<=state.position_quantity_version
                or c.mode=='fixed_quantity' and (c.quantity!=action.quantity or c.dynamic_contract_id is not None)
                or c.mode=='dynamic_position' and (not seed.rules.verified or not seed.rules.dynamic_full_position_stop
                                                   or c.dynamic_contract_id!=seed.rules.dynamic_stop_contract_id)):
                raise ExitContractError('Persisted stop coverage contract invalid')
        elif action.stop_confirmed:
            raise ExitContractError('Confirmed stop lacks a coverage contract')
        if action.target_confirmed and (action.kind not in ('CANCEL','RECONCILE') or action.status!='FILLED'):
            raise ExitContractError('Invalid control target confirmation')
        raw=action.model_copy(update={'action_id':'0'*64,'status':'INTENT','filled_quantity':ZERO,
            'acknowledged_quantity':ZERO,'stop_confirmed':False,'terminal_status':None,'terminal_quantity':None,
            'confirmed_coverage':None,'target_confirmed':False})
        expected=hashlib.sha256((state.seed_digest+state.policy_digest+digest(raw)).encode()).hexdigest()
        if action.sequence!=index or action.action_id!=expected:
            raise ExitContractError('Persisted action identity/terms mismatch')
    refreshed=_refresh_coverage(state)
    if (state.protection_status,state.protection_covered_quantity,state.protection_coverage_version)!=(
        refreshed.protection_status,refreshed.protection_covered_quantity,refreshed.protection_coverage_version):
        raise ExitContractError('Claimed protection coverage disagrees with confirmed capacity')
    return seed,policy,state


def initialize_exit(seed: ExitSeed, policy: ExitPolicy) -> ExitResult:
    if type(seed) is not ExitSeed or type(policy) is not ExitPolicy:
        raise ExitContractError('Exit initialization requires confirmed fills, never a Signal')
    seed=ExitSeed.model_validate(seed.model_dump());policy=ExitPolicy.model_validate(policy.model_dump())
    fill=seed.first_fill
    if fill.occurred_at>fill.received_at: raise ExitContractError('Future fill timestamp')
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)):
        initial_r=abs(fill.price-seed.plan.original_stop)
        state=ExitState(seed_digest=digest(seed),policy_digest=digest(policy),position_id=seed.position_id,
            symbol=seed.plan.symbol,side=seed.plan.side,version=0,phase='OPEN',entry_action_id=fill.entry_action_id,
            opened_at=fill.occurred_at,original_quantity=fill.quantity,remaining_quantity=fill.quantity,
            actual_average_entry=fill.price,entry_notional=fill.price*fill.quantity,
            remaining_entry_cost=fill.price*fill.quantity,remaining_average_entry=fill.price,
            frozen_r_anchor_entry=fill.price,frozen_initial_r=initial_r,original_stop=seed.plan.original_stop,
            current_stop=seed.plan.original_stop,entry_fees=fill.fee_usdt,realized_net_pnl=-fill.fee_usdt,
            favorable_extreme=fill.price,last_received_at=fill.received_at,last_confirmation_event=fill.event_id,
            event_receipts=((fill.event_id,digest(fill)),),fill_facts=(_fill_fact(fill,fill.entry_action_id),))
        if _crossed(state,fill.price,state.original_stop):
            state=_copy(state,emergency_reason='FILL_ALREADY_BEYOND_INITIAL_STOP')
        reasons=[];state=_refresh_coverage(_drive(state,seed,policy,fill.received_at,reasons))
        return ExitResult(state=state,actions=state.actions,reason_codes=tuple(reasons) or ('FIRST_FILL_R_FROZEN',))


def apply_event(seed: ExitSeed, policy: ExitPolicy, state: ExitState, event: Event) -> ExitResult:
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)):
        seed,policy,state=_validate(seed,policy,state)
        if type(event) not in (EntryFill,EntrySealed,MarketEvent,ActionReceipt,ExitFill,ProtectionLost,RecoveryRequired):
            raise ExitContractError('Explicit confirmed event type required')
        event=EVENTS.validate_python(event.model_dump())
        receipt=next((h for key,h in state.event_receipts if key==event.event_id),None)
        if receipt:
            if receipt!=digest(event): raise ExitContractError('Event ID content conflict')
            return ExitResult(state=state,actions=(),reason_codes=('DUPLICATE_EVENT_IGNORED',))
        if event.position_id!=state.position_id or event.received_at<state.last_received_at:
            raise ExitContractError('Wrong position or non-monotonic ingestion time')
        start=len(state.actions)
        state=_copy(state,version=state.version+1,last_received_at=event.received_at,
            event_receipts=(*state.event_receipts,(event.event_id,digest(event))))
        reasons=[]
        if isinstance(event,EntryFill):
            fact=_fill_fact(event,event.entry_action_id)
            if not _is_duplicate_fill(state,fact):
                if state.entry_sealed or event.entry_action_id!=state.entry_action_id:
                    state=_fault(state,'UNEXPECTED_ENTRY_FILL_REQUIRES_RECONCILIATION')
                elif event.occurred_at>event.received_at:
                    state=_fault(state,'ENTRY_FILL_TIME_INCONSISTENT')
                else:
                    late=any(f.action_id!=state.entry_action_id for f in state.fill_facts)
                    q=state.original_quantity+event.quantity;notional=state.entry_notional+event.price*event.quantity
                    remaining=state.remaining_quantity+event.quantity
                    cost=state.remaining_entry_cost+event.price*event.quantity
                    state=_copy(state,original_quantity=q,remaining_quantity=remaining,
                        remaining_entry_cost=cost,remaining_average_entry=cost/remaining,
                        position_quantity_version=state.position_quantity_version+1,
                        opened_at=min(state.opened_at,event.occurred_at),
                        actual_average_entry=notional/q,entry_notional=notional,entry_fees=state.entry_fees+event.fee_usdt,
                        realized_net_pnl=state.realized_net_pnl-event.fee_usdt,fill_facts=(*state.fill_facts,fact),
                        last_confirmation_event=event.event_id,
                        emergency_reason='LATE_OPENING_FILL_AFTER_EXIT' if late else state.emergency_reason)
        elif isinstance(event,EntrySealed):
            if event.entry_action_id!=state.entry_action_id or event.total_filled_quantity!=state.original_quantity:
                state=_fault(state,'ENTRY_SEAL_QUANTITY_MISMATCH')
            elif not state.entry_sealed:
                q1=(state.original_quantity*policy.tp1_fraction/seed.rules.quantity_step).to_integral_value(rounding=ROUND_FLOOR)*seed.rules.quantity_step
                q2=(state.original_quantity*policy.tp2_fraction/seed.rules.quantity_step).to_integral_value(rounding=ROUND_FLOOR)*seed.rules.quantity_step
                state=_copy(state,entry_sealed=True,tp1_planned=q1,tp2_planned=q2,
                    runner_planned=state.original_quantity-q1-q2,last_confirmation_event=event.event_id)
        elif isinstance(event,MarketEvent):
            if event.observed_at>event.received_at:
                state=_copy(state,last_market=None)
                reasons.append('FUTURE_MARKET_DISCARDED')
            elif state.last_market is None or event.observed_at>state.last_market.observed_at:
                state=_copy(state,last_market=event)
                price=_quote(state,policy,event.received_at)
                if price is not None:
                    peak=max(state.favorable_extreme,price) if state.side=='LONG' else min(state.favorable_extreme,price)
                    state=_copy(state,favorable_extreme=peak)
            else: reasons.append('OLDER_MARKET_IGNORED')
        elif isinstance(event,ExitFill):
            if event.occurred_at>event.received_at or event.occurred_at<state.opened_at:
                state=_fault(state,'EXIT_FILL_TIME_INCONSISTENT')
            else: state=_apply_fill(state,event)
        elif isinstance(event,ActionReceipt): state=_receipt(seed,state,event)
        elif isinstance(event,ProtectionLost):
            action=_action(state,event.action_id)
            if action is None or action.kind not in STOP_KINDS:
                state=_fault(state,'UNRECOGNIZED_PROTECTION_LOSS')
            elif action.lifecycle_terminal:
                # A delayed loss/UNKNOWN report for a retired stop cannot rebind
                # the current protection. Validate any final total without reactivation.
                if event.status!='UNKNOWN' and event.cumulative_filled_quantity is not None:
                    state=_receipt(seed,state,ActionReceipt(event_id=event.event_id,position_id=event.position_id,
                        received_at=event.received_at,action_id=event.action_id,status='CANCELED',
                        cumulative_filled_quantity=event.cumulative_filled_quantity))
            elif event.action_id==state.protection_action_id or action.status in ('INTENT','UNKNOWN','SETTLING'):
                unknown=event.status=='UNKNOWN' or event.cumulative_filled_quantity is None
                state=_copy(state,emergency_reason='PROTECTION_LOST',protection_status='UNKNOWN' if unknown else 'MISSING',
                    protection_action_id=event.action_id if unknown else None,last_confirmation_event=event.event_id)
                if unknown: state=_set_action(state,action.model_copy(update={'status':'UNKNOWN'}))
                else:
                    state=_receipt(seed,state,ActionReceipt(event_id=event.event_id,position_id=event.position_id,
                        received_at=event.received_at,action_id=event.action_id,status='CANCELED',
                        cumulative_filled_quantity=event.cumulative_filled_quantity))
        elif isinstance(event,RecoveryRequired):
            ids=tuple(a.action_id for a in state.actions if a.kind in (*EXIT_KINDS,*STOP_KINDS) and a.status in BUSY)
            state=_copy(state,recovery_pending_action_ids=ids,last_market=None)
            for key in ids:
                attempts=[a for a in state.actions if a.kind=='RECONCILE' and a.target_action_id==key]
                if _control_failures(attempts)>=policy.max_control_attempts:
                    state=_fault(state,'RECONCILE_CONTROL_ATTEMPTS_EXHAUSTED')
                else:
                    state=_emit(state,'RECONCILE','RESTART_RECONCILE_ORIGINAL_ACTION',target=key)
        if isinstance(event,EntrySealed) and state.entry_sealed and not state.faults:
            state=_resolve_controls(state,state.entry_action_id,'FILLED')
        if isinstance(event,(ActionReceipt,ExitFill,ProtectionLost)) and not state.faults:
            action=_action(state,event.action_id)
            if action is not None and action.kind not in ('CANCEL','RECONCILE'):
                state=_resolve_controls(state,action.action_id,action.status)
        if isinstance(event,(ActionReceipt,ExitFill,ProtectionLost)) and state.recovery_pending_action_ids:
            action=_action(state,event.action_id)
            if action is not None and _target_settled(action) and not state.faults:
                state=_copy(state,recovery_pending_action_ids=tuple(k for k in state.recovery_pending_action_ids if k!=event.action_id))
        state=_refresh_coverage(_drive(state,seed,policy,event.received_at,reasons))
        if state.faults:
            state=_copy(state,phase='PROTECTION_REQUIRED')
            reasons.extend(state.faults)
        state=ExitState.model_validate(state.model_dump())
        return ExitResult(state=state,actions=state.actions[start:],reason_codes=tuple(dict.fromkeys(reasons)) or ('CONFIRMED_EVENT_REDUCED',))
