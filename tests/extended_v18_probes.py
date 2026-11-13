"""Bounded synthetic mutation, graph-scaling and IP-policy regression checks."""
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
sys.path[:0] = [str(ROOT/'tests'), str(SCRIPTS)]
import test_submission_review as s
import test_evidence_export as e
import test_investigation as i
import test_channel_check as c
import test_consumer_materials as u
import test_dashboard_queue_integration as d
import test_audit_regressions as a
import capture_web as capture
import record_integrity as integrity

VALUES = [None, False, True, 0, 1, -1, 1.5, '', ' ', 'bad', [], {}, [None], ['x'], {'id':'x'}]
EXPECTED = (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError)


def paths(value, path=()):
    yield path
    if isinstance(value, dict):
        for key, child in value.items():
            yield from paths(child, path+(key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from paths(child, path+(index,))


def changed(seed, path, value):
    if not path:
        return copy.deepcopy(value)
    result = copy.deepcopy(seed)
    cursor = result
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = copy.deepcopy(value)
    return result


summaries = []
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    submission, approval = s.fixture(root)
    export_case, export_review = e.fixtures(root)
    inputs = [
        ('submission_review', submission, lambda x: s.m.review(x, 'a1', approval, root, s.NOW)),
        ('evidence_export', export_case, lambda x: e.m.prepare(x, export_review, root, e.NOW)),
        ('investigation', i.fixture(), lambda x: i.m.assess(x, i.NOW)),
        ('channel_check', c.fixture(), lambda x: c.m.check(x, 'a1', c.NOW)),
        ('consumer_materials', u.fixture(), lambda x: u.m.prepare(x, u.NOW)),
        ('case_dashboard', d.DashboardQueueIntegration().case(), lambda x: d.d.m.project(x, d.d.NOW)),
        ('followup_queue', a.unresolved_claim('interrupted'), lambda x: a.module.build_queue(x, a.NOW)),
    ]
    for name, seed, call in inputs:
        count = 0
        exceptions, mutations, unsafe = [], [], []
        for path in paths(seed):
            for value in VALUES:
                current = changed(seed, path, value)
                before = copy.deepcopy(current)
                try:
                    output = call(current)
                    if isinstance(output, dict) and (output.get('authorizes_submission') is True
                            or output.get('authorizes_publication') is True):
                        unsafe.append(list(path))
                except EXPECTED:
                    pass
                except Exception as exc:
                    exceptions.append({'path':list(path), 'value':value,
                                       'type':type(exc).__name__, 'message':str(exc)[:160]})
                if current != before:
                    mutations.append(list(path))
                count += 1
        summaries.append({'module':name, 'variants':count, 'unexpected_exceptions':exceptions,
                          'input_mutations':mutations, 'authorization_escalations':unsafe})
print('MUTATION_AUDIT_JSON='+json.dumps({'python':sys.version.split()[0], 'results':summaries,
      'variants':sum(row['variants'] for row in summaries)}, ensure_ascii=True))

ipresults = []
for address in ['127.0.0.1', '10.0.0.1', '192.0.0.8', '192.0.0.9', '64:ff9b::a00:1',
                '64:ff9b:1::a00:1', '2002:7f00:1::', '2001:db8::1']:
    lookup = lambda *args, **kw: [(socket.AF_INET6 if ':' in address else socket.AF_INET,
                                  socket.SOCK_STREAM, 6, '', (address, 443))]
    try:
        capture.public_addresses('synthetic.example', lookup)
        accepted = True
    except ValueError:
        accepted = False
    ipresults.append({'address':address, 'accepted_as_public':accepted})
    if accepted != (address == '192.0.0.9'):
        raise AssertionError('IP policy regression: '+address)
print('IP_POLICY_AUDIT_JSON='+json.dumps({'python':sys.version.split()[0], 'results':ipresults,
                                       'actual_network_requests':0}))

scaling = []
original = integrity.identifier
for size in [64, 128, 256, 512, 4096]:
    calls = [0]
    def counted(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)
    actions = {'a'+str(n):{'id':'a'+str(n), 'corrects_action_id':None if n == 0 else 'a'+str(n-1)}
               for n in range(size)}
    integrity.identifier = counted
    start = time.perf_counter()
    try:
        family = s.m.family(actions, 'a'+str(size-1))
    finally:
        integrity.identifier = original
    if len(family) != size or not size <= calls[0] <= 3*size:
        raise AssertionError('Correction graph validation must remain bounded and linear')
    scaling.append({'nodes':size, 'identifier_validations':calls[0],
                    'seconds':round(time.perf_counter()-start, 6)})
print('GRAPH_SCALING_AUDIT_JSON='+json.dumps(scaling))
if any(row['unexpected_exceptions'] or row['input_mutations'] or row['authorization_escalations']
       for row in summaries):
    raise SystemExit(1)
