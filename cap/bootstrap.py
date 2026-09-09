"""Seed only the new Cap service. Never overwrite operator configuration."""
from pathlib import Path
import json
import os


def seed(root: Path, templates: Path, *, service: str) -> dict:
    if service != 'Hermes-Cap':
        raise RuntimeError('Cap image is restricted to the Hermes-Cap service')
    home = root / '.hermes'
    home.mkdir(parents=True, exist_ok=True)
    (root / 'cap' / 'workspace').mkdir(parents=True, exist_ok=True)
    (root / '.codex').mkdir(mode=0o700, exist_ok=True)
    created = []
    for name in ('config.yaml', 'SOUL.md'):
        target = home / name
        try:
            with target.open('x') as destination:
                destination.write((templates / name).read_text())
            created.append(name)
        except FileExistsError:
            pass
    return {'event': 'cap_bootstrap', 'created': created,
            'worker_dispatch_enabled_by_bootstrap': False}


if __name__ == '__main__':
    print(json.dumps(seed(Path('/data'), Path('/app/cap'),
                          service=os.environ.get('RAILWAY_SERVICE_NAME', ''))))
