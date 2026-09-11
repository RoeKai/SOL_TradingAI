"""Narrow immutable JSON outputs; intentionally no diagnostic runner imports."""
import json
from decimal import Decimal


def serial(value):
    if isinstance(value,Decimal):return str(value)
    if hasattr(value,'model_dump'):return value.model_dump(mode='json')
    if isinstance(value,dict):return {k:serial(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [serial(v) for v in value]
    return value


def write(path,value):
    with path.open('x',encoding='utf-8') as out:
        json.dump(serial(value),out,sort_keys=True,indent=2,ensure_ascii=False,allow_nan=False)
        out.write('\n')


def emit(value):
    print(json.dumps(serial(value),sort_keys=True,allow_nan=False),flush=True)
