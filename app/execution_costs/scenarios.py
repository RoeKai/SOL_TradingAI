"""Exactly the accepted conditional path simulator with quantity-bound costs."""
from app.historical_replay.scenarios import evaluate_scenarios as original_paths
from app.offline_paper.storage import digest
from app.historical_replay.data import HistoricalError
from .models import QuantifiedScenarios


def evaluate_scenarios(plan,setup,snapshot,q):
    if plan.materialized_quantity!=q:
        raise HistoricalError('MATERIALIZED_SCENARIO_QUANTITY_MISMATCH')
    value=original_paths(plan,setup,snapshot,q).model_dump()
    value.update(version='quantity-consistent-scenarios/v1',scope='OFFLINE_EXECUTION_PRICE_COST_MODEL',evaluation_id='0'*64,
        limitations=('Conditional ordered paths, NOT statistical expectation or guaranteed return',
            'Costs materialized at this exact original quantity; funding allowance is not actual settlement',
            'Fixed-R runner reference is a visible structure position, not its exit price',
            'Assumed spread/fees/liquidity; no real liquidation, ADL, book or account capability'))
    result=QuantifiedScenarios.model_validate(value)
    return result.model_copy(update={'evaluation_id':digest(result)})
