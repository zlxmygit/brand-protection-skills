"""Verify transport bytes before applying the reviewed repair payload."""
import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent
expected = ['74ead46ea469513399ce7bd92f50a4afc669d7f0469a76182bc493bb05507229', 'db25a5571c5d8ac1e2d45d70c6a5d5784d18a15ceba68f8dd8d27987a3149688', '9618f58767109ebd523bcc22a8a60c5f94f040cc962aa5e386831ebbb6a43888', 'e21d8aa211ab6347de01ce085cbbfdad576b913c14936b47f85937b920507f6b']
repairs = {'FzlRRZbKZu':'FzlRRZyKZu', 'c3ZvFfeFcz':'c3ZvF6cz', 'srxlhbjl2YYEVfC0':'srxlhbjl2YEVfC0', 'LoUz54UzNfb':'LoUz54VzNfb', 'DxzJPI5aVbuDrTn':'DxzJPI5aVDrTn', 'yPU0zaz0j':'yPU0az0j'}
parts = []
for number, path in enumerate(sorted(root.glob('payload*.b64'))):
    value = path.read_text(encoding='ascii').strip()
    for old, new in repairs.items():
        value = value.replace(old, new)
    actual = hashlib.sha256(value.encode()).hexdigest()
    print('TRANSPORT_PART', number, len(value), actual, flush=True)
    if actual != expected[number]:
        print('TRANSPORT_DIAGNOSTIC='+json.dumps({'part':number,'blocks':[hashlib.sha256(value[i:i+100].encode()).hexdigest()[:12] for i in range(0,len(value),100)]}),flush=True)
        raise ValueError('Transport chunk checksum mismatch; do not apply')
    parts.append(value)
assert len(parts) == 4
raw = base64.b64decode(''.join(parts), validate=True)
assert hashlib.sha256(raw).hexdigest() == 'bca7cbd02b9fd4a12215a332b52f8bd332d1dce59e8b027884db8987f8a25ce5'
with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    assert len(archive.infolist()) == 8 and archive.testzip() is None
    for info in archive.infolist():
        path = Path(info.filename)
        assert not path.is_absolute() and '..' not in path.parts and '\\' not in info.filename
        assert info.file_size < 1000000
    archive.extractall(root)
print('REPAIR_PAYLOAD_VERIFIED')
