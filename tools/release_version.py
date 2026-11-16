"""Validate the repository's consecutive patch-only release policy."""
import re


def validate_patch_release(version):
    if not isinstance(version, dict):
        raise ValueError('invalid release version configuration')
    parsed = {}
    for field in ('tag', 'previous'):
        value = version.get(field)
        match = re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',
                             value if isinstance(value, str) else '')
        if match is None:
            raise ValueError('invalid release '+field)
        parsed[field] = tuple(int(part) for part in match.groups())
    current, previous = parsed['tag'], parsed['previous']
    if current[:2] != previous[:2] or current[2] != previous[2] + 1:
        raise ValueError('release must increment only the patch version by one')
    return version['tag']
