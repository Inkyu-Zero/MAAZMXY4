"""Narrow schema extension owned by this project's patched task loader."""
from copy import deepcopy


def extend_task_schema(schema):
    result=deepcopy(schema)
    properties=result.get('definitions',{}).get('taskItem',{}).get('properties')
    if properties is None or 'entry' not in properties:
        raise ValueError('Interface schema changed: taskItem.entry is missing')
    properties.setdefault('default_add',dict(type=['boolean','null'],
        description='Whether new queues add this task automatically; false requires explicit + addition.'))
    # Existing MFA host-owned repetition fields are not Maa pipeline fields.
    properties.setdefault('repeatable',dict(type=['boolean','null']))
    properties.setdefault('repeat_count',dict(type=['integer','null']))
    return result
