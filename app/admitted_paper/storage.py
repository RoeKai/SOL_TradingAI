"""Explicit new instance/schema; reuse the accepted transaction implementation."""
from app.offline_paper.storage import Store, TABLES, ADMITTED_TABLES, put, digest
from .models import AdmittedSettings, PROVIDER, FUNDING


class AdmittedStore(Store):
    settings_model=AdmittedSettings
    application_id=1397705785
    schema_version=2
    table_names=(*TABLES,*ADMITTED_TABLES)
    directory='offline-admitted-runs'

    @classmethod
    def create(cls,workspace,run_id,settings,bundle):
        store=super().create(workspace,run_id,settings,bundle)
        # Explicit initialization only. Missing registry on restart is corruption,
        # not an invitation to re-register a caller-supplied provider.
        with store.transaction() as db:
            record=provider_record(settings.instance_id)
            put(db,'paper_providers',PROVIDER,dict(record,digest=digest(record)))
        return store


def provider_record(instance_id):
    return dict(provider=PROVIDER,version=1,instance_id=instance_id,scope='SYNTHETIC_OFFLINE',
        rule='confirmed-three-point-swings/recent-four-direction-volume-resonance/v1',
        funding_model=FUNDING,funding_basis='this_simulated_contract_has_no_periodic_funding',
        maximum_holding_seconds=3600)
