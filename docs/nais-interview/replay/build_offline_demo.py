"""Embed the sanitized replay JSON into offline-demo.html (self-contained, opens from file://, no fetch).

observations.json goes into __OBSERVATIONS_JSON__ exactly as before; the optional supplemental
gang-observations.json goes into a separate __GANG_JSON__ slot (`null` when absent), so the base payload is unchanged.
Run: rtk proxy python3 docs/nais-interview/replay/build_offline_demo.py
"""
from pathlib import Path
import json

REPLAY = Path(__file__).resolve().parent


def embed(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')


def build(obs=None):
    obs = obs if obs is not None else json.loads((REPLAY / 'observations.json').read_text())
    gang_path = REPLAY / 'gang-observations.json'
    gang = json.loads(gang_path.read_text()) if gang_path.exists() else None
    html = (REPLAY / 'offline-demo.template.html').read_text()
    html = html.replace('__OBSERVATIONS_JSON__', embed(obs)).replace('__GANG_JSON__', embed(gang))
    (REPLAY.parent / 'offline-demo.html').write_text(html)


if __name__ == '__main__':
    build()
