#!/usr/bin/env python3
"""Prepare private consumer-communication drafts from recorded official channels."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from string import Template
import sys
import tempfile
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investigation import read_json, text, timestamp, domain_ascii, ids
from submission_review import indexed
from evidence_export import digest
from capture_web import private_root, write_private
from record_integrity import check_identity, evidence_closure

ASSETS = Path(__file__).resolve().parents[1] / 'assets'
GATES = {'official_channels_reviewed','public_text_reviewed','privacy_reviewed'}
KINDS = {'website','store','support'}
SNAPSHOT_VERSION = '1.1'
DRAFT_HEADER = '草稿：发布前仍需人工复核，请勿直接外发。\n\n'


def official_url(value, domains):
    text(value, 'official channel URL', 4096)
    if not value.isascii() or value != value.strip() or '\\' in value or any(ord(c)<32 or ord(c)==127 for c in unquote(value)):
        raise ValueError('Use an explicit ASCII HTTPS channel URL')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None,443) or parsed.query or parsed.fragment):
        raise ValueError('Official channel URL cannot include credentials, query or fragment')
    host = domain_ascii(parsed.hostname)
    if host not in domains:
        raise ValueError('Channel host is not in the recorded official domain list')
    return value


def prepare(case, now=None):
    if not isinstance(case,dict):
        raise ValueError('Case must be an object')
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now requires timezone')
    now = now.astimezone(timezone.utc)
    case_id = text(case.get('case_id'),'case_id',200)
    brand_id = text(case.get('brand_id'),'brand_id',200)
    brand = text(case.get('brand'),'brand',200)
    if any(token in brand for token in ('<','>','{{','}}','://')):
        raise ValueError('Use a plain single-line brand name')
    domains = ids(case.get('official_domains'),'official_domains',allow_empty=False)
    for domain in domains:
        domain_ascii(domain)
    profile = case.get('consumer_communications')
    if not isinstance(profile,dict) or profile.get('version') != '1.0' or profile.get('brand_id') != brand_id:
        raise ValueError('Expected a same-brand consumer communication profile')
    check_identity(case, profile, 'consumer communication profile')
    checked = timestamp(profile.get('checked_at'),'checked_at')
    before = timestamp(profile.get('review_before'),'review_before')
    if not checked <= now < before:
        raise ValueError('Official channel verification needs renewal')
    text(profile.get('verified_by'),'verified_by',200)
    text(profile.get('verification_source'),'verification_source')
    evidence = indexed(case,'evidence')
    channels = indexed(profile,'channels')
    if not 1 <= len(channels) <= 30:
        raise ValueError('Record between 1 and 30 reviewed official channels')
    seen, used_evidence, rows = set(), set(), []
    for key, channel in sorted(channels.items()):
        check_identity(case, channel, 'official channel ' + key)
        kind = channel.get('kind')
        if not isinstance(kind,str) or kind not in KINDS or channel.get('verification_status') != 'verified':
            raise ValueError('Each channel needs a supported kind and recorded verification')
        url = official_url(channel.get('url'),domains)
        if url in seen:
            raise ValueError('Duplicate official channel URL')
        seen.add(url)
        verified = timestamp(channel.get('verified_at'),'channel verified_at')
        if verified > checked:
            raise ValueError('Channel changed after the profile review')
        refs = ids(channel.get('evidence_ids'),'channel evidence_ids',allow_empty=False)
        used_evidence.update(refs)
        rows.append((key,kind,url))
    # Validate the union once: parents shared across channels count once and
    # every ancestor contributes to the recorded-source review fingerprint.
    used_evidence = evidence_closure(case, evidence, sorted(used_evidence))
    support_id = text(profile.get('support_channel_id'),'support_channel_id',200)
    if support_id not in channels or channels[support_id]['kind'] != 'support':
        raise ValueError('Choose an explicitly verified support channel')
    templates = read_json(ASSETS/'consumer-templates.json')
    if templates.get('version') != '1.0' or set(templates.get('templates',{})) != {'official-channels','customer-reply','report-intake'}:
        raise ValueError('Unexpected communication template version')
    descriptions = {'website':'网站','store':'店铺','support':'客服'}
    context = {'brand':brand, 'checked_at':checked.isoformat(),
               'channels':'\n'.join(descriptions[kind]+'：'+url for _,kind,url in rows),
               'support_url':channels[support_id]['url']}
    drafts = {}
    for name, content in templates['templates'].items():
        if not isinstance(content,str):
            raise ValueError('Template must be text')
        drafts[name+'.txt'] = DRAFT_HEADER + Template(content).substitute(context)
    source = {'snapshot_version':SNAPSHOT_VERSION,'case_id':case_id,'brand_id':brand_id,'brand':brand,'official_domains':sorted(domains),
              'profile':profile,'evidence':[evidence[key] for key in sorted(used_evidence)],
              'templates':templates,'drafts':drafts}
    fingerprint = hashlib.sha256(json.dumps(source,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode('ascii')).hexdigest()
    return {'snapshot_version':SNAPSHOT_VERSION,'case_id':case_id,'brand_id':brand_id,'snapshot_sha256':fingerprint,'drafts':drafts,
            'state':'draft_needs_review','authorizes_publication':False,'sends_messages':False}


def match_approval(prepared, approval, now):
    if not isinstance(approval,dict) or approval.get('version') != '1.0':
        raise ValueError('Expected an explicit approval record')
    for field in ('case_id','brand_id'):
        if approval.get(field) != prepared[field]:
            raise ValueError('Approval belongs to another case or brand')
    if digest(approval.get('snapshot_sha256')) != prepared['snapshot_sha256']:
        raise ValueError('Approval does not match these channels, sources and draft bytes')
    for field in ('reviewed_by','authorization_source'):
        text(approval.get(field),field)
    approved = timestamp(approval.get('approved_at'),'approved_at')
    expires = timestamp(approval.get('expires_at'),'expires_at')
    if not approved <= now < expires:
        raise ValueError('Draft approval is future-dated or expired')
    checks = approval.get('checks')
    if not isinstance(checks,dict) or set(checks)!=GATES or any(value is not True for value in checks.values()):
        raise ValueError('Official channels, text and privacy require explicit review')


def generate(case, output_root, approval=None, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    prepared = prepare(case,now)
    if approval is not None:
        match_approval(prepared,approval,now)
        prepared['state'] = 'review_matches_draft'
    root = private_root(output_root)
    directory = Path(tempfile.mkdtemp(prefix='consumer-drafts-',dir=root))
    try:
        hashes = {}
        for name, content in prepared['drafts'].items():
            data = content.encode('utf-8')
            path = directory/name
            write_private(path,data)
            if path.read_bytes()!=data:
                raise ValueError('Stored draft differs from prepared text')
            hashes[name] = hashlib.sha256(data).hexdigest()
        receipt = {key:value for key,value in prepared.items() if key!='drafts'}
        receipt.update(directory=str(directory),files=hashes,
                       limitations=['Draft review records do not authenticate identity or grant publication authority',
                                    'Official-channel evidence must be independently verified before public use',
                                    'Fingerprint binds recorded evidence lineage, not freshly read evidence file bytes'])
        write_private(directory/'internal-review.json',json.dumps(receipt,ensure_ascii=True,indent=2).encode('ascii'))
        return receipt
    except BaseException:
        shutil.rmtree(directory)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True,type=Path)
    parser.add_argument('--output-root',required=True,type=Path)
    parser.add_argument('--approval',type=Path)
    args = parser.parse_args()
    try:
        result = generate(read_json(args.case),args.output_root,read_json(args.approval) if args.approval else None)
    except (OSError,ValueError,TypeError,KeyError,OverflowError,RecursionError):
        print(json.dumps({'errors':['consumer_draft_validation_failed'],'authorizes_publication':False}))
        return 1
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0


if __name__=='__main__':sys.exit(main())
