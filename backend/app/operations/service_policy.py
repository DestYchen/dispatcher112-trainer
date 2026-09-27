"""Allowed installation services and resource bounds, shared with local tools."""

SERVICES = frozenset(
    {
        "backend",
        "frontend",
        "gateway",
        "worker",
        "sip_worker",
        "telephony",
        "languagetool",
        "ollama",
        "postgres",
        "redis",
        "backup",
        "languagetool_tls",
        "ollama_tls",
    }
)
RESTART_ONLY = frozenset({"backend", "frontend", "gateway", "postgres", "redis", "backup"})
MIN_MEMORY = {"languagetool": 1024, "ollama": 4096, "backend": 512, "worker": 2048}


def valid_limits(service: str, cpus: object, memory_mb: object) -> bool:
    return (
        service in SERVICES
        and type(cpus) in {int, float}
        and isinstance(cpus, (int, float))
        and 0.5 <= cpus <= 64
        and type(memory_mb) is int
        and MIN_MEMORY.get(service, 128) <= memory_mb <= 65536
    )
