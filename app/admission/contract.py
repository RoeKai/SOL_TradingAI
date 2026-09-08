"""Future Paper boundary contract, NOT a broker or an executable capability.

Stage 5 leaves the old Signal/Paper route untouched. A later integration MUST
call this under the execution lock, supply a new trusted snapshot, atomically
reserve risk/entry slots, and persist decision consumption before Paper writes.
Content hashes and a pure function alone cannot provide authentication or replay
protection; those stateful operations deliberately do not exist in this stage.
"""

from decimal import Decimal

from pydantic import ValidationError

from app.setups.models import TradeSetup
from app.setups.rr_models import RRCalculation
from app.setups.scorecard_models import Scorecard
from .engine import admit_trade
from .models import (AdmissionContractError, AdmissionDecision, AdmissionRequest,
                     ExchangeConstraints, PaperRiskSnapshot, PositiveAmount, Record)
from .policy import AdmissionPolicy


class _PaperQuantity(Record):
    quantity: PositiveAmount


def require_paper_admission(decision: AdmissionDecision, *, setup: TradeSetup, rr: RRCalculation,
        scorecard: Scorecard, account: PaperRiskSnapshot, exchange: ExchangeConstraints,
        request: AdmissionRequest, policy: AdmissionPolicy, evaluated_at: float,
        quantity: Decimal) -> AdmissionDecision:
    """Recompute and match every input; never accept a naked Signal or altered decision.

    Exact quantity is required because even decreasing quantity can invalidate
    net RR with fixed funding. Request a fresh decision for another quantity.
    The return value still has execution_authority=none_until_paper_integration.
    """
    if type(decision) is not AdmissionDecision:
        raise AdmissionContractError('Paper contract requires AdmissionDecision, never TradeSetup/Signal/dict')
    try:
        decision=AdmissionDecision.model_validate(decision.model_dump())
        requested=_PaperQuantity(quantity=quantity).quantity
    except (ValidationError,TypeError,ValueError) as error:
        raise AdmissionContractError('Malformed decision or requested quantity') from error
    if decision.result=='REJECT' or decision.binding is None or decision.valid_until is None:
        raise AdmissionContractError('Rejected decisions cannot reach the future Paper boundary')
    if requested!=decision.max_quantity:
        raise AdmissionContractError('Quantity differs from the exact RR-checked size; request a new decision')
    kwargs=dict(account=account,exchange=exchange,request=request,policy=policy)
    expected=admit_trade(setup,rr,scorecard,**kwargs,evaluated_at=decision.evaluated_at)
    if decision!=expected:
        raise AdmissionContractError('Decision/content/revision/policy mismatch or forged permission')
    if evaluated_at<decision.evaluated_at or evaluated_at>=decision.valid_until:
        raise AdmissionContractError('Decision expired or consumer time predates its issuance')
    refreshed=admit_trade(setup,rr,scorecard,**kwargs,evaluated_at=evaluated_at)
    if refreshed.result=='REJECT' or refreshed.max_quantity!=requested:
        raise AdmissionContractError('Fresh risk evaluation no longer permits this exact Paper size')
    return refreshed
