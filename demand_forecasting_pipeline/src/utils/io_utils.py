import os
import json
import joblib


def save_json(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def save_pickle(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    joblib.dump(obj, path)


def load_pickle(path):
    return joblib.load(path)


def save_dataframe(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)


def ensure_tuple(keys):
    return keys if isinstance(keys, tuple) else (keys,)


def pair_mask(df, group_keys, pair_keys):
    mask = df[group_keys[0]] == pair_keys[0]
    for i in range(1, len(group_keys)):
        mask = mask & (df[group_keys[i]] == pair_keys[i])
    return mask
