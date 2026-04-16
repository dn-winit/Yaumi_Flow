import os
import yaml


def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    return cfg


def ensure_dirs(cfg):
    for key, val in cfg.get("paths", {}).items():
        if key.endswith("_dir"):
            os.makedirs(val, exist_ok=True)
