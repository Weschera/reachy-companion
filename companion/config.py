from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def load_profiles() -> dict:
    with open(ROOT / "profiles.yaml") as f:
        return yaml.safe_load(f)


def resolve_brain(cfg: dict) -> dict:
    """Merge the active model entry into the brain config."""
    brain = dict(cfg["brain"])
    models = brain.get("models", {})
    active = models.get(brain.get("active"))
    if active:
        brain.update(active)
    brain.setdefault("api_key", "none")
    return brain
