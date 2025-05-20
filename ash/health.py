from typing import TypedDict

import psutil


class Health(TypedDict):
    cpu: float
    mem: float


def get_health() -> Health:
    mem = psutil.virtual_memory()
    mem_usage = 100 * ((mem.total - mem.free - mem.buffers - mem.cached) / mem.total)
    return {
        "cpu": psutil.cpu_percent(),
        "mem": mem_usage,
    }
