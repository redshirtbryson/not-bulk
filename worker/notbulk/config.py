from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    """Load the NotBulk config as a plain nested dict.

    Raises FileNotFoundError with a clear message if the file is absent.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"config file not found at {p.resolve()} "
            f"(pass an explicit path or run from the repo root)"
        )
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)
