import telemetry_pb2 as pb # Файл, сгенерированный из твоего telemetry.proto
import logging

logger = logging.getLogger("Deserializer")

def protobuf_to_dict(binary_data):
    """
    Превращает бинарный пакет Protobuf в словарь Python.
    """
    try:
        # Создаем пустой объект сообщения
        packet = pb.TelemetryPacket()
        # Десериализуем байты
        packet.ParseFromString(binary_data)
        
        # Превращаем в плоский словарь для Polars
        return {
            "node_id": packet.node_id,
            "timestamp": packet.timestamp,
            "cpu_now": packet.cpu_now,
            "ram_rss": packet.ram_rss,
            "active_jobs": packet.active_jobs,
            "cpu_delta": packet.cpu_delta,
            "ram_delta": packet.ram_delta,
            "net_err_delta": packet.net_err_delta
        }
    except Exception as e:
        logger.error(f"Ошибка десериализации пакета: {e}")
        return None
