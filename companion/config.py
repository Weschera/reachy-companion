from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def load_profiles() -> dict:
    with open(ROOT / "profiles.yaml") as f:
        return yaml.safe_load(f)
