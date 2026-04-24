"""Mock L3 producer that emits synthetic HeteroData snapshots to Kafka."""

from __future__ import annotations

import logging
import json
import time

import torch
from confluent_kafka import Producer
from torch_geometric.data import HeteroData


def generate_synthetic_graph() -> HeteroData:
    """Generate a synthetic heterogeneous graph snapshot.

    Returns:
        HeteroData: Randomized graph with host/vm/job node features and edges.
    """

    data = HeteroData()

    num_hosts = 3
    num_vms = 5
    num_jobs = 10

    # Random node features by entity type.
    data["host"].x = torch.randn(num_hosts, 16)
    data["vm"].x = torch.randn(num_vms, 8)
    data["job"].x = torch.randn(num_jobs, 4)

    # L4 model also has switch readout, so we include a minimal switch node set.
    data["switch"].x = torch.randn(2, 12)

    # VM -> Host mapping: each VM is allocated on a random host.
    vm_indices = torch.arange(num_vms, dtype=torch.long)
    vm_to_host = torch.randint(0, num_hosts, (num_vms,), dtype=torch.long)
    data[("vm", "allocated_on", "host")].edge_index = torch.stack([vm_indices, vm_to_host], dim=0)

    # Job -> VM mapping: each job is allocated on a random VM.
    job_indices = torch.arange(num_jobs, dtype=torch.long)
    job_to_vm = torch.randint(0, num_vms, (num_jobs,), dtype=torch.long)
    data[("job", "allocated_on", "vm")].edge_index = torch.stack([job_indices, job_to_vm], dim=0)

    # Host -> Switch connectivity for relation expected by L4 model.
    host_indices = torch.arange(num_hosts, dtype=torch.long)
    host_to_switch = torch.randint(0, int(data["switch"].x.size(0)), (num_hosts,), dtype=torch.long)
    data[("host", "connected_to", "switch")].edge_index = torch.stack(
        [host_indices, host_to_switch],
        dim=0,
    )

    return data


def heterodata_to_payload(data: HeteroData) -> dict[str, dict[str, list[list[float]]]]:
    """Convert ``HeteroData`` to JSON-safe payload.

    Args:
        data: Graph snapshot to serialize.

    Returns:
        dict[str, dict[str, list[list[float]]]]: JSON-ready payload.
    """

    x_dict_payload = {
        node_type: node_store.x.detach().cpu().tolist()
        for node_type, node_store in data.node_items()
    }
    edge_index_payload = {
        "__".join(edge_type): edge_store.edge_index.detach().cpu().tolist()
        for edge_type, edge_store in data.edge_items()
    }
    return {
        "x_dict": x_dict_payload,
        "edge_index_dict": edge_index_payload,
    }


def main() -> None:
    """Run synthetic graph producer loop."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logger = logging.getLogger(__name__)

    producer = Producer({"bootstrap.servers": "localhost:9092"})
    topic = "L3_IN"

    logger.info("Starting Mock L3 producer. Target topic: %s", topic)
    for _ in range(10):
        graph = generate_synthetic_graph()
        payload = json.dumps(heterodata_to_payload(graph), ensure_ascii=True).encode("utf-8")
        producer.produce(topic=topic, value=payload)
        producer.flush()
        logger.info("Synthetic graph snapshot sent to L3_IN")
        time.sleep(5)

    logger.info("Mock L3 producer finished sending snapshots.")


if __name__ == "__main__":
    main()
