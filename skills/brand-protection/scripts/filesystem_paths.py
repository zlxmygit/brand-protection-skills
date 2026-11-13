"""Native local paths with narrow macOS aliases and no user-controlled links."""
import os
from pathlib import Path, PureWindowsPath
import stat
import sys

# macOS installs these root-owned aliases. Other symlinks remain forbidden.
SYSTEM_ALIASES = {'/tmp': '/private/tmp', '/var': '/private/var'}
WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    prefix + suffix for prefix in ('COM', 'LPT') for suffix in '123456789\u00b9\u00b2\u00b3'}


def validate_windows_path(value):
    """Reject ambiguous/device paths before any filesystem access, on every Python."""
    path = PureWindowsPath(value)
    if path.drive.startswith('\\\\'):
        raise ValueError('Use a local drive path, not UNC or device namespaces')
    if bool(path.drive) != bool(path.root):
        raise ValueError('Drive-relative and root-relative Windows paths are ambiguous')
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        if (part == '..' or part.endswith((' ', '.'))
                or any(c in '<>:"|?*' or ord(c) < 32 for c in part)
                or part.split('.', 1)[0].upper() in WINDOWS_RESERVED):
            raise ValueError('Unsupported Windows path component')
    return path


def system_alias_target(path, link, platform, owner_uid):
    """Match only exact root-owned macOS aliases; never trust the basename alone."""
    expected = SYSTEM_ALIASES.get(str(path)) if platform == 'darwin' else None
    if expected is None or owner_uid != 0 or link not in (expected, expected.lstrip('/')):
        return None
    return Path(expected)


def checked_existing_path(value):
    """Resolve an existing path after checking every supplied ancestor.

    Resolving first would hide symlinks/junctions. A private parent directory must
    also be protected against concurrent replacement by other OS accounts.
    """
    supplied = Path(value).expanduser()
    if '..' in supplied.parts:
        raise ValueError('Parent traversal is not allowed')
    if os.name == 'nt':
        validate_windows_path(str(supplied))
    supplied = supplied.absolute()
    for entry in (*reversed(supplied.parents), supplied):
        metadata = entry.lstat()
        if entry.is_symlink() or stat.S_ISLNK(metadata.st_mode):
            if sys.platform != 'darwin' or str(entry) not in SYSTEM_ALIASES:
                raise ValueError('User-controlled symbolic links are not allowed')
            expected = system_alias_target(entry, os.readlink(entry), sys.platform,
                                           getattr(metadata, 'st_uid', None))
            if expected is None or entry.resolve(strict=True) != expected:
                raise ValueError('User-controlled symbolic links are not allowed')
        elif getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            # Includes junctions on Python 3.9, before Path.is_junction existed.
            raise ValueError('Windows reparse points and junctions are not allowed')
    return supplied.resolve(strict=True)
