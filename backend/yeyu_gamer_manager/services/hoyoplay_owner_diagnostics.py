"""Bounded counters only. These diagnostics never authenticate a UIA owner."""
import json

CONTRACT = 'hoyoplay.uia-owner-diagnostic@1'
FIELDS = frozenset(('contract','source','taskId','helperPid','sequence','expectedPid','observedPid',
    'nodeIndex','parentIndex','depth','relation','controlTypeId','rootHwnd','nodeHwnd'))


def owner_diagnostic(value, *, source, task_id, helper_pid, sequence):
    if (not isinstance(value, dict) or set(value) != FIELDS or value.get('contract') != CONTRACT
            or value.get('source') != source or value.get('taskId') != task_id
            or value.get('helperPid') != helper_pid or value.get('sequence') != sequence):
        return None
    ranges = {'helperPid':(1,2**32-1),'sequence':(0,10000),'expectedPid':(1,2**32-1),
        'observedPid':(-1,2**32-1),'nodeIndex':(0,255),'parentIndex':(-1,254),'depth':(0,12),
        'controlTypeId':(-1,50049),'rootHwnd':(1,2**63-1),'nodeHwnd':(-2**31,2**63-1)}
    if any(type(value.get(key)) is not int or not low <= value[key] <= high for key,(low,high) in ranges.items()):
        return None
    if value['controlTypeId'] != -1 and value['controlTypeId'] < 50000:
        return None
    root=value['nodeIndex']==0 and value['parentIndex']==-1 and value['depth']==0
    descendant=0 <= value['parentIndex'] < value['nodeIndex'] and value['depth']>0
    if not ((root and value['relation']=='root') or (descendant and value['relation']=='descendant')):
        return None
    if len(json.dumps(value,separators=(',',':')).encode('utf-8'))>1024:
        return None
    return dict(value)
