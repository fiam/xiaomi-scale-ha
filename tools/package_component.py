"""Build a deterministic component package from a verified source archive."""
import io
import json
import zipfile

def package_component(source_payload, source):
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source_payload)) as upstream, zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        prefix = 'xiaomi-scale-ha-' + source['revision'] + '/custom_components/xiaomi_scale_ha/'
        for name in sorted(upstream.namelist()):
            if name.startswith(prefix) and not name.endswith('/'):
                relative = name[len(prefix):]
                if '__pycache__' in relative or relative.endswith('.pyc'):
                    continue
                item = zipfile.ZipInfo(relative, date_time=(1980,1,1,0,0,0))
                item.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(item, upstream.read(name))
        archive.writestr(zipfile.ZipInfo('provenance.json', date_time=(1980,1,1,0,0,0)), json.dumps(source,indent=2)+'\n')
    return buf.getvalue()
