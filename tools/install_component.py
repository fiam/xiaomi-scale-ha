"""Install a component ZIP from stdin, verifying SHA256 and retaining the prior build.

Usage: python install_component.py EXPECTED_SHA256 [HA_CONFIG_DIRECTORY] < component.zip
Default configuration directory: /config. Restart HA after Python code changes.
"""
import hashlib, io, json, pathlib, sys, time, zipfile
expected = sys.argv[1]
payload = sys.stdin.buffer.read(2_000_001)
if len(payload) > 2_000_000 or hashlib.sha256(payload).hexdigest() != expected:
    raise SystemExit('Package checksum or size mismatch')
root = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else '/config')
target = root / 'custom_components/xiaomi_scale_ha'
if target.is_symlink():
    raise SystemExit('Refusing symlink target')
stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
record = root / '.xiaomi-scale-installs' / (stamp + '-' + expected[:12])
record.mkdir(parents=True, exist_ok=False)
stage = record / 'staged'
stage.mkdir()
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    for item in archive.infolist():
        path = pathlib.PurePosixPath(item.filename)
        if path.is_absolute() or '..' in path.parts or item.file_size > 1_000_000:
            raise SystemExit('Invalid archive member')
        output = stage / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(archive.read(item))
        output.chmod(0o644)
manifest = json.loads((stage / 'manifest.json').read_text())
if manifest['domain'] != 'xiaomi_scale_ha':
    raise SystemExit('Wrong integration')
(record / 'package.zip').write_bytes(payload)
previous = record / 'previous'
if target.exists():
    target.rename(previous)
try:
    target.parent.mkdir(parents=True, exist_ok=True)
    stage.rename(target)
except BaseException:
    if previous.exists(): previous.rename(target)
    raise
metadata = {'sha256':expected, 'version':manifest['version'], 'installed_at':stamp,
            'source':json.loads((target / 'provenance.json').read_text()) if (target / 'provenance.json').exists() else None,
            'previous':str(previous) if previous.exists() else None}
(record / 'installation.json').write_text(json.dumps(metadata, indent=2)+'\n')
print(json.dumps(metadata))
