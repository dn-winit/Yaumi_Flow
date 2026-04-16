



def compute_target_encoding(train_df, group_keys, target_col, smoothing):
    global_mean = train_df[target_col].mean()
    pair_stats = train_df.groupby(group_keys)[target_col].agg(["mean", "count"]).reset_index()
    pair_stats.columns = group_keys + ["pair_mean", "pair_count"]
    pair_stats["te_pair_mean"] = (
        (pair_stats["pair_count"] * pair_stats["pair_mean"] + smoothing * global_mean)
        / (pair_stats["pair_count"] + smoothing)
    )
    return pair_stats[group_keys + ["te_pair_mean"]], global_mean


def apply_target_encoding(df, group_keys, encoding_df, global_mean):
    merged = df.merge(encoding_df, on=group_keys, how="left")
    merged["te_pair_mean"] = merged["te_pair_mean"].fillna(global_mean)
    return merged
