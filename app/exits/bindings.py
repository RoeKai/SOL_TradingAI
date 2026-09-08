"""Read-only historical plan snapshot, not new-entry approval or order authority."""

from app.admission.models import AdmissionDecision, fingerprint
from app.setups.models import TradeSetup
from app.setups.rr_models import RRCalculation
from app.setups.scorecard_models import Scorecard
from .models import ExitContractError, PlanSnapshot


def capture_plan(setup: TradeSetup, rr: RRCalculation, scorecard: Scorecard,
                 admission: AdmissionDecision) -> PlanSnapshot:
    types=(TradeSetup,RRCalculation,Scorecard,AdmissionDecision)
    values=(setup,rr,scorecard,admission)
    if any(type(v) is not t for v,t in zip(values,types)):
        raise ExitContractError('Explicit historical stage records required')
    setup,rr,scorecard,admission=(t.model_validate(v.model_dump()) for t,v in zip(types,values))
    for obj in (rr,scorecard):
        if (obj.setup_id,obj.plan_version,obj.symbol,obj.side)!=(setup.setup_id,setup.plan_version,setup.symbol,setup.side):
            raise ExitContractError('Historical plan identity mismatch')
    if admission.binding is not None and (admission.binding.setup,admission.binding.rr,admission.binding.scorecard)!=(
            fingerprint(setup),fingerprint(rr),fingerprint(scorecard)):
        raise ExitContractError('Historical decision content mismatch')
    return PlanSnapshot(setup_id=setup.setup_id,plan_version=setup.plan_version,symbol=setup.symbol,
        side=setup.side,original_stop=str(setup.initial_stop.price),
        original_targets=tuple((t.target_id,str(t.price),str(t.fraction)) for t in setup.targets),
        setup_digest=fingerprint(setup),rr_digest=fingerprint(rr),scorecard_digest=fingerprint(scorecard),
        admission_digest=fingerprint(admission),admission_result=admission.result)
