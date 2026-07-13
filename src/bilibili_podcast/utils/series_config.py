from pathlib import Path
from ..config.models import SeriesConfig


def load_series_configs(config_dir: str | Path) -> list[SeriesConfig]:
    config_path = Path(config_dir)
    configs = []
    for path in sorted(config_path.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        configs.append(SeriesConfig.from_yaml(path))
    return configs
