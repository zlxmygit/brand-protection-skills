"""Fail-closed release preflight. Only an HTTP 404 means absent."""
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(path):
    request = Request('https://api.github.com/' + path, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
        'Accept': 'application/vnd.github+json', 'User-Agent': 'brand-protection-release'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('GitHub response exceeds limit')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('Unexpected GitHub response')
            return value
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError('GitHub request failed with HTTP ' + str(exc.code)) from exc
    except URLError as exc:
        raise RuntimeError('GitHub request unavailable') from exc


def preflight(repository, tag, source, fetch=get_json):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('Invalid release tag')
    if not re.fullmatch(r'[0-9a-f]{40}', source):
        raise ValueError('Invalid source commit')
    prefix = 'repos/' + repository
    if fetch(prefix + '/releases/tags/' + tag) is not None:
        raise ValueError('Refusing to replace an existing release')
    existing = fetch(prefix + '/git/ref/tags/' + tag)
    if existing is not None:
        obj = existing.get('object', {})
        if not isinstance(obj, dict) or obj.get('type') != 'commit' or obj.get('sha') != source:
            raise ValueError('Tag does not match tested source')
    return {'tag_exists': existing is not None, 'tag': tag, 'source': source}


def main():
    try:
        version = json.loads(Path('release/version.json').read_text(encoding='utf-8'))
        result = preflight(os.environ['GH_REPO'], version['tag'], os.environ['SOURCE'])
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print('Release preflight failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
