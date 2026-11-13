"""Process-only verified-image transport adapter around the fixed official CLI."""
import json
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(os.environ['EVO_SCORER_ROOT'])
sys.path.insert(0, str(ROOT))
import docker  # noqa: E402,F401 - preserve the historical transport imports after setting the frozen source path.
from docker.models.images import ImageCollection  # noqa: E402

ALLOWED = {
    'xingyaoww/sweb.eval.x86_64.conan-io_s_conan-15109': 'sha256:0d0885fcb5607347cabc7256fd30ce4dfb092063c54a420b7f4ed445aeda20e3',
    'xingyaoww/sweb.eval.x86_64.dask_s_dask-9531': 'sha256:3bfe422ab58729fc9f4765f79076d3f22db8dba3c16d63101d83ac339f7c44b4',
}

def cached_pull(self, repository, tag=None, **kwargs):
    name = repository.removesuffix(':latest')
    if name not in ALLOWED or tag not in (None, 'latest'):
        raise RuntimeError('offline cache adapter refuses an unapproved image')
    image = self.get(name + ':latest')
    if image.id != ALLOWED[name]:
        raise RuntimeError('offline cache image config ID mismatch')
    with Path('cache-transport.jsonl').open('a') as stream:
        stream.write(json.dumps({'requested': repository, 'actual_config_id': image.id,
                                 'transport': 'verified_local_cache_no_network'}) + '\n')
    return image

ImageCollection.pull = cached_pull
sys.argv[0] = str(ROOT / 'evaluate_instance.py')
runpy.run_path(str(ROOT / 'evaluate_instance.py'), run_name='__main__')
