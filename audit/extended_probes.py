"""Bounded synthetic mutation, graph-scaling, IP-policy and document inspection."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tests'),str(ROOT/'skills/brand-protection/scripts')]
import test_submission_review as s
import test_evidence_export as e
import test_investigation as i
import test_channel_check as c
import test_consumer_materials as u
import test_dashboard_queue_integration as d
import test_audit_regressions as a
import capture_web as capture

VALUES = [None, False, True, 0, 1, -1, 1.5, '', ' ', 'bad', [], {}, [None], ['x'], {'id':'x'}]
EXPECTED = (OSError,ValueError,TypeError,KeyError,OverflowError,RecursionError)

def paths(value, path=()):
    yield path
    if isinstance(value,dict):
        for key, child in value.items():
            yield from paths(child,path+(key,))
    elif isinstance(value,list):
        for index,child in enumerate(value):
            yield from paths(child,path+(index,))

def changed(seed, path, value):
    if not path: return copy.deepcopy(value)
    result = copy.deepcopy(seed)
    cursor = result
    for key in path[:-1]: cursor=cursor[key]
    cursor[path[-1]]=copy.deepcopy(value)
    return result

summaries=[]
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory)
    submission, approval=s.fixture(root)
    export_case, export_review=e.fixtures(root)
    inputs=[
        ('submission_review',submission,lambda x:s.m.review(x,'a1',approval,root,s.NOW)),
        ('evidence_export',export_case,lambda x:e.m.prepare(x,export_review,root,e.NOW)),
        ('investigation',i.fixture(),lambda x:i.m.assess(x,i.NOW)),
        ('channel_check',c.fixture(),lambda x:c.m.check(x,'a1',c.NOW)),
        ('consumer_materials',u.fixture(),lambda x:u.m.prepare(x,u.NOW)),
        ('case_dashboard',d.DashboardQueueIntegration().case(),lambda x:d.d.m.project(x,d.d.NOW)),
        ('followup_queue',a.unresolved_claim('interrupted'),lambda x:a.module.build_queue(x,a.NOW)),
    ]
    for name,seed,call in inputs:
        count=0;exceptions=[];mutations=[];unsafe=[]
        for path in paths(seed):
            for value in VALUES:
                current=changed(seed,path,value)
                before=copy.deepcopy(current)
                try:
                    output=call(current)
                    if isinstance(output,dict) and (output.get('authorizes_submission') is True or output.get('authorizes_publication') is True):
                        unsafe.append(list(path))
                except EXPECTED:
                    pass
                except Exception as exc:
                    exceptions.append({'path':list(path),'value':value,'type':type(exc).__name__,'message':str(exc)[:160]})
                if current!=before:mutations.append(list(path))
                count+=1
        summaries.append({'module':name,'variants':count,'unexpected_exceptions':exceptions,
                          'input_mutations':mutations,'authorization_escalations':unsafe})
print('MUTATION_AUDIT_JSON='+json.dumps({'python':sys.version.split()[0],'results':summaries,'variants':sum(r['variants'] for r in summaries)},ensure_ascii=True))

ipresults=[]
for address in ['127.0.0.1','10.0.0.1','192.0.0.8','192.0.0.9','64:ff9b::a00:1','64:ff9b:1::a00:1','2002:7f00:1::','2001:db8::1']:
    lookup=lambda *args,**kw:[(socket.AF_INET6 if ':' in address else socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))]
    try:
        capture.public_addresses('synthetic.example',lookup)
        accepted=True
    except ValueError:accepted=False
    ipresults.append({'address':address,'accepted_as_public':accepted})
print('IP_POLICY_AUDIT_JSON='+json.dumps({'python':sys.version.split()[0],'results':ipresults,'actual_network_requests':0}))

scaling=[]
original=s.m.text
for size in [64,128,256,512]:
    calls=[0]
    def counted(*args,**kwargs):
        calls[0]+=1
        return original(*args,**kwargs)
    actions={'a'+str(n):{'id':'a'+str(n),'corrects_action_id':None if n==0 else 'a'+str(n-1)} for n in range(size)}
    s.m.text=counted
    start=time.perf_counter()
    try: s.m.family(actions,'a'+str(size-1))
    finally:s.m.text=original
    scaling.append({'nodes':size,'validated_references':calls[0],'seconds':round(time.perf_counter()-start,6)})
print('GRAPH_SCALING_AUDIT_JSON='+json.dumps(scaling))

if os.environ.get('PRINT_DOCUMENTS')=='1':
    names = ['.gitattributes','.gitignore','AGENTS.md','CHANGELOG.md','CODE_OF_CONDUCT.md','CONTRIBUTING.md','LICENSE','SECURITY.md','SUPPORT.md','docs/installation.md','release/version.json',
        '.github/ISSUE_TEMPLATE/bug_report.yml','.github/ISSUE_TEMPLATE/channel_update.yml','.github/ISSUE_TEMPLATE/config.yml','.github/ISSUE_TEMPLATE/feature_request.yml','.github/PULL_REQUEST_TEMPLATE.md','.github/workflows/tests.yml',
        '.agents/skills/zh-tech-writing/SKILL.md','.agents/skills/zh-tech-writing/LICENSE','.agents/skills/zh-tech-writing/references/manual-structure.md','.agents/skills/zh-tech-writing/references/typography.md',
        'skills/brand-protection/agents/openai.yaml','skills/brand-protection/assets/follow-up-policy.json','skills/brand-protection/assets/intake-form.md','skills/brand-protection/assets/monitoring-prompt.md',
        'skills/brand-protection/references/intake.md','skills/brand-protection/references/patents-designs.md','skills/brand-protection/references/case-format.md','skills/brand-protection/references/dashboard.md','skills/brand-protection/references/tools.md','skills/brand-protection/references/queue-safety.md',
        'docs/releases/v1.1.0.md','docs/releases/v1.2.0.md']
    for name in names:
        print('DOCUMENT_BEGIN '+name)
        for line,text in enumerate((ROOT/name).read_text(encoding='utf-8').splitlines(),1):
            print(str(line)+': '+text)
        print('DOCUMENT_END '+name)
    references={}
    for path in sorted((ROOT/'skills/brand-protection/scripts').glob('*.py')):
        import ast
        parsed=ast.parse(path.read_bytes())
        references[path.name]={node.name:[node.lineno,node.end_lineno] for node in parsed.body if isinstance(node,(ast.FunctionDef,ast.ClassDef))}
    print('FUNCTION_RANGES_JSON='+json.dumps(references))
if any(r['unexpected_exceptions'] or r['input_mutations'] or r['authorization_escalations'] for r in summaries):
    raise SystemExit(1)
