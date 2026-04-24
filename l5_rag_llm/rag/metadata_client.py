"""Metadata retrieval and caching utilities for the L5 layer."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import redis


class MetadataRetriever:
    """Retrieve node metadata with Redis-backed cache.

    The retriever simulates requests to CMDB/scheduler sources by generating
    deterministic mock metadata and caching it in Redis for reuse.
    """

    def __init__(self, redis_client: redis.Redis, logger: logging.Logger) -> None:
        """Initialize metadata retriever dependencies.

        Args:
            redis_client: Redis client used for metadata cache operations.
            logger: Logger instance used for warnings and diagnostics.
        """

        self.redis_client = redis_client
        self.logger = logger

    def get_node_context(self, node_type: str, node_id: int) -> Dict[str, Any]:
        """Return cached or generated context for a node entity.

        Cache strategy:
            1) Read JSON payload from Redis key
               ``cache:metadata:{node_type}:{node_id}``.
            2) On miss, generate deterministic mock metadata by node type.
            3) Save generated metadata back to Redis with TTL=3600 seconds.

        Redis failures are non-fatal; method returns generated metadata and logs
        a warning when cache read/write is unavailable.

        Args:
            node_type: Entity type (for example ``host``, ``vm``, or ``job``).
            node_id: Numeric entity identifier.

        Returns:
            Dict[str, Any]: Context dictionary for LLM enrichment.
        """

        cache_key = f"cache:metadata:{node_type}:{node_id}"

        try:
            cached_data = self.redis_client.get(cache_key)
            if cached_data:
                if isinstance(cached_data, bytes):
                    cached_str = cached_data.decode("utf-8")
                else:
                    cached_str = str(cached_data)
                return json.loads(cached_str)
        except redis.ConnectionError as exc:
            self.logger.warning("Redis unavailable during metadata cache read: %s", exc)

        context: Dict[str, Any]
        if node_type == "host":
            context = {
                "hostname": f"cn-{node_id}.jinr.ru",
                "rack": f"Rack-{node_id % 10}",
                "ip": f"10.100.0.{node_id}",
                "hardware": "2x Intel Xeon, 128GB RAM",
                "status": "active",
            }
        elif node_type == "vm":
            context = {
                "vm_name": f"vm-payload-{node_id}",
                "owner_group": "group_physics",
                "allocated_on_host": f"cn-{node_id % 100}.jinr.ru",
            }
        elif node_type == "job":
            context = {
                "job_name": f"hydrodynamics_sim_{node_id}",
                "user": "ivanov_a",
                "queue": "cpu_queue",
                "priority": "high",
            }
        else:
            context = {"info": f"Unknown entity {node_type}_{node_id}"}

        try:
            self.redis_client.setex(cache_key, 3600, json.dumps(context, ensure_ascii=True))
        except redis.ConnectionError as exc:
            self.logger.warning("Redis unavailable during metadata cache write: %s", exc)

        return context
