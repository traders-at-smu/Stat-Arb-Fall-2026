# Nonsecret runtime configuration; safety locks and strategy math are not
# settings.
import json
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]


def load_settings():
    path = Path(
        os.environ.get('BROKER_EXECUTION_CONFIG', ROOT / 'config.json')
    )
    return json.loads(path.read_text())


def research_root():
    configured = load_settings()['research_directory']
    if configured is None:
        return ROOT / 'ian_original' / 'ResidualBeta_share'
    path = Path(configured).expanduser()
    return path if path.is_absolute() else ROOT / path
