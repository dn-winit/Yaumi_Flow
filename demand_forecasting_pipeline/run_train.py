import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from src.pipelines.train_pipeline import run_training


if __name__ == "__main__":
    cfg_path = os.path.join(ROOT, "config", "config.yaml")
    if len(sys.argv) > 1:
        cfg_path = sys.argv[1]
    run_training(cfg_path)
