#!/usr/bin/env python3
"""Generate a private static case snapshot. No network, controls or case writes."""
import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investigation import read_json, index_rows, timestamp, text
from capture_web import private_root, write_private

STAGES = {'researching':'调查中','draft':'草稿','awaiting_evidence':'待补证','ready_for_review':'待审',
          'authorized':'已记录授权','submitted':'已提交','acknowledged':'已受理',
          'needs_information':'机构要求补证','rejected':'已驳回','appealed':'已申诉',
          'counter_notified':'收到反通知','action_reported':'机构报告已采取措施',
          'verified_effective':'台账记录已复核生效','verified_partial':'部分生效',
          'not_verified':'尚未验证','correction_pending':'待更正','closed_no_action':'关闭但未处置','withdrawn':'已撤回'}
TERMINAL = {'verified_effective','closed_no_action','withdrawn'}
CLOSED_DEADLINES = {'completed','resolved','cancelled','superseded'}
QUEUES = {'due_tasks':'到期候选','held_tasks':'阻塞任务','deferred_tasks':'未来任务',
          'recovery_tasks':'恢复事项','coverage_gaps':'覆盖缺口','deadline_conflicts':'期限冲突'}
SAFE_QUEUE_FIELDS = ('id','task_id','action_id','target_id','deadline_id','kind','reason','due_at','eligible_at')
STYLE = 'body{font:16px/1.65 system-ui,sans-serif;max-width:1120px;margin:32px auto;padding:0 20px;color:#17212b;background:#f8fafc}h1,h2,h3{line-height:1.3}section{background:white;padding:20px;margin:20px 0;border:1px solid #cbd5e1;border-radius:8px}table{border-collapse:collapse;width:100%;margin:12px 0}th,td{border-bottom:1px solid #dbe2ea;text-align:left;vertical-align:top;padding:8px;overflow-wrap:anywhere}th{background:#edf2f7}p{max-width:90ch}.notice{border-left:4px solid #b45309;padding:10px;background:#fff7ed}pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#475569}'


def label(value, limit=300):
    if not isinstance(value, str):
        return '未知'
    cleaned = ''.join(c for c in value if ord(c) >= 32 and not 0x7f <= ord(c) <= 0x9f
                      and c not in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + '…'
    return cleaned or '未知'


def run_queue(case, now):
    from followup_queue import build_queue
    return build_queue(case, now)


def project(case, now, queue_builder=None):
    if not isinstance(case, dict):
        raise ValueError('Case must be an object')
    text(case.get('case_id'), 'case_id', 200)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now requires timezone')
    now = now.astimezone(timezone.utc)
    errors = []
    actions = index_rows(case, 'actions', errors)
    targets = index_rows(case, 'targets', errors)
    deadlines = index_rows(case, 'deadlines', errors)
    rows, stages = [], Counter()
    for key, action in sorted(actions.items()):
        status = action.get('status')
        if not isinstance(status, str) or status not in STAGES:
            errors.append({'section':'actions','id':key,'error':'unknown status'})
            status = 'unknown'
        stages[status] += 1
        rows.append({'id':key,'status':status,'stage':STAGES.get(status,'未知状态'),
                     'corrects_action_id':label(action.get('corrects_action_id')) if action.get('corrects_action_id') is not None else '',
                     'open_correction': action.get('corrects_action_id') is not None and status not in TERMINAL})
    due_rows = []
    for key, deadline in deadlines.items():
        state = deadline.get('status')
        if isinstance(state,str) and state in CLOSED_DEADLINES:
            continue
        try:
            due = timestamp(deadline.get('due_at'), 'due_at')
            due_state = 'overdue' if due < now else 'upcoming'
            due_text = due.isoformat()
        except (ValueError, TypeError, OverflowError):
            due_state, due_text = 'unknown','待核实'
            errors.append({'section':'deadlines','id':key,'error':'unknown or invalid due_at'})
        due_rows.append({'id':key,'action_id':label(deadline.get('action_id')),
                         'due_at':due_text,'state':due_state})
    due_rows.sort(key=lambda row: (row['state'] != 'overdue', row['due_at'], row['id']))
    queue_builder = run_queue if queue_builder is None else queue_builder
    try:
        queue = queue_builder(case, now)
        if (not isinstance(queue,dict) or not isinstance(queue.get('state'),str)
                or any(key not in queue for key in (*QUEUES, 'errors'))):
            raise ValueError('Invalid queue report')
        queue_state = label(queue.get('state'))
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        queue, queue_state = {}, 'unavailable'
        errors.append({'section':'queue','error':'queue unavailable; not an empty workload'})
    projected_queues, counts = {}, {}
    for section in QUEUES:
        items = queue.get(section, [])
        if not isinstance(items,list) or any(not isinstance(item,dict) for item in items):
            errors.append({'section':section,'error':'invalid queue section'})
            projected_queues[section], counts[section] = [], None
            continue
        counts[section] = len(items)
        projected_queues[section] = [{key:label(item[key]) for key in SAFE_QUEUE_FIELDS if key in item} for item in items]
    queue_errors = queue.get('errors', [])
    if isinstance(queue_errors,list):
        errors.extend(row for row in queue_errors if isinstance(row,dict))
    else:
        errors.append({'section':'queue','error':'invalid queue errors'})
    if queue_state == 'unavailable':
        counts = {section:None for section in QUEUES}
    return {'case_id':label(case['case_id']), 'checked_at':now.isoformat(), 'read_only':True,
            'queue_state':queue_state, 'target_count':len(targets), 'action_count':len(actions),
            'stages':dict(stages), 'actions':rows, 'deadlines':due_rows,
            'queue_counts':counts, 'queues':projected_queues,
            'errors':[{'section':label(e.get('section')), 'id':label(e.get('id'))} for e in errors],
            'open_corrections':sum(row['open_correction'] for row in rows),
            'authorizes_submission':False}


def table(headers, rows):
    head = '<tr>' + ''.join('<th>'+escape(h)+'</th>' for h in headers) + '</tr>'
    body = ''.join('<tr>'+''.join('<td>'+escape(str(cell))+'</td>' for cell in row)+'</tr>' for row in rows)
    return '<table><thead>'+head+'</thead><tbody>'+body+'</tbody></table>'


def render(views, row_limit=200):
    if type(row_limit) is not int or not 1 <= row_limit <= 1000:
        raise ValueError('row_limit must be between 1 and 1000')
    style_hash = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode('ascii')
    policy = "default-src 'none'; script-src 'none'; base-uri 'none'; form-action 'none'; style-src 'sha256-"+style_hash+"'"
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta http-equiv="Content-Security-Policy" content="'+escape(policy,quote=True)+'">',
             '<meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>案件只读快照</title><style>'+STYLE+'</style></head><body><h1>案件只读快照</h1>',
             '<p class="notice">私有报告。状态来自台账，未核验真实处理结果。页面不联网、不自动刷新，也不提供提交或修改入口。</p>']
    seen = Counter(view.get('case_id') for view in views)
    for number, view in enumerate(views, 1):
        case_id = label(view.get('case_id'))
        parts.append('<section><h2>'+escape(str(number)+'. '+case_id)+'</h2>')
        if seen[view.get('case_id')] > 1:
            parts.append('<p class="notice">输入存在重复案件 ID，分别展示，未合并或累计。</p>')
        if view.get('input_error'):
            parts.append('<p class="notice">该输入无法读取或解析，不能视为没有待办。</p></section>')
            continue
        parts.append('<p>检查时间：'+escape(view['checked_at'])+'；队列状态：'+escape(view['queue_state'])+'。</p>')
        parts.append('<p>已登记目标：'+str(view['target_count'])+'；行动：'+str(view['action_count'])+'；未结更正：'+str(view['open_corrections'])+'。</p>')
        parts.append('<h3>行动阶段</h3><p>已提交、已受理、机构报告采取措施和台账记录复核生效分别统计。</p>')
        parts.append(table(['台账状态','数量'],[(STAGES.get(key,'未知状态'),count) for key,count in sorted(view['stages'].items())]))
        parts.append(table(['行动 ID','阶段','更正的原行动'],[(label(r['id']),r['stage'],r['corrects_action_id']) for r in view['actions'][:row_limit]]))
        if len(view['actions']) > row_limit:
            parts.append('<p>行动明细已截断；统计保留全部已读取记录。</p>')
        parts.append('<h3>未完成期限</h3><p>按台账时间展示，即使未启用周期跟进也保留。未知期限需要核实。</p>')
        parts.append(table(['期限 ID','行动 ID','时间（UTC）','状态'],[(label(r['id']),r['action_id'],r['due_at'],r['state']) for r in view['deadlines'][:row_limit]]))
        if len(view['deadlines']) > row_limit:
            parts.append('<p>期限明细已截断，请检查原台账。</p>')
        for key, heading in QUEUES.items():
            count = view['queue_counts'][key]
            parts.append('<h3>'+heading+'：'+('未知' if count is None else str(count))+'</h3>')
            rows = view['queues'][key]
            for row in rows[:row_limit]:
                parts.append('<pre>'+escape(json.dumps(row,ensure_ascii=False))+'</pre>')
            if len(rows) > row_limit:
                parts.append('<p>本类明细已截断，请读取完整队列输出。</p>')
        parts.append('<h3>需要修复的记录：'+str(len(view['errors']))+'</h3>')
        parts.append(table(['位置','记录 ID'],[(e['section'],e['id']) for e in view['errors'][:row_limit]]))
        if len(view['errors']) > row_limit:
            parts.append('<p>错误明细已截断。</p>')
        parts.append('<p>错误详情请在原台账及只读队列中核对。未展示的证据、联系方式和授权信息不代表不存在。</p></section>')
    parts.append('</body></html>')
    return ''.join(parts)


def save_dashboard(views, output_root, row_limit=200):
    root = private_root(output_root)
    data = render(views,row_limit).encode('utf-8')
    directory = Path(tempfile.mkdtemp(prefix='dashboard-',dir=root))
    try:
        path = directory/'index.html'
        write_private(path,data)
        if path.read_bytes() != data:
            raise ValueError('Dashboard verification failed')
        return {'path':str(path),'sha256':hashlib.sha256(data).hexdigest(), 'authorizes_submission':False,
                'read_only':True, 'private_report':True}
    except BaseException:
        shutil.rmtree(directory)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True,action='append',type=Path)
    parser.add_argument('--output-root',required=True,type=Path)
    parser.add_argument('--row-limit',type=int,default=200)
    args = parser.parse_args()
    views, now = [], datetime.now(timezone.utc)
    for number,path in enumerate(args.case,1):
        try:
            views.append(project(read_json(path),now))
        except (OSError,ValueError,TypeError,RecursionError):
            views.append({'case_id':'输入 '+str(number),'input_error':True})
    try:
        receipt = save_dashboard(views,args.output_root,args.row_limit)
    except (OSError,ValueError,TypeError):
        print(json.dumps({'errors':['dashboard_write_failed']}))
        return 1
    print(json.dumps(receipt,ensure_ascii=True))
    return 1 if any(view.get('input_error') or view.get('errors') for view in views) else 0


if __name__ == '__main__': sys.exit(main())
