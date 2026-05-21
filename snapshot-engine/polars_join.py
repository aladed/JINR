import polars as pl
from datetime import datetime

def align_telemetry_streams(cpu_df, network_df):
    """
    Выполняет Point-in-Time Join (join_asof) для синхронизации потоков.
    Это необходимо, так как метрики от L1 приходят асинхронно.
    """

    # Сортировка по времени обязательна для join_asof
    cpu_df = cpu_df.sort("timestamp")
    network_df = network_df.sort("timestamp")

    # join_asof находит ближайшую по времени запись из второго потока
    # 'backward' означает, что для текущего CPU мы ищем последнюю известную метрику сети
    enriched_df = cpu_df.join_asof(
        network_df,
        on="timestamp",
        by="node_id",
        strategy="backward"
    )

    # Очистка данных: заполняем возможные NaN (пропуски) нулями или средним
    final_df = enriched_df.with_columns([
        pl.col("net_err_delta").fill_null(0),
        pl.col("cpu_delta").fill_nan(0)
    ])

    return final_df

def prepare_for_gnn(df):
    """
    Финальная подготовка признаков (Features) перед отправкой Владу в ML-слой.
    """
    # Группируем данные в 5-минутные окна (Bucketing), о чем ты писал в дипломе
    return df.group_by_dynamic("timestamp", every="5m").agg([
        pl.col("cpu_now").mean().alias("avg_cpu"),
        pl.col("cpu_delta").max().alias("peak_gradient"),
        pl.col("ram_rss").last()
    ])
