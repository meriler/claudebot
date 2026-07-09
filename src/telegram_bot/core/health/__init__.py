"""Self-healing network layer and heartbeat client.

See .docs/specs/TZ-2026-05-18-klava-self-heal-heartbeat.md. Two deviations
from the spec, decided in the 2026-06-11 stability review:

- FR-9 (sys.exit(1) after 30 network failures) is replaced by degraded mode:
  a long Telegram outage is normal for the deployment environment, and a
  crash-loop would kill in-flight Claude sessions for no gain. The process
  exits only when session recreation itself breaks repeatedly (internal
  failure, not a network one).
- FR-13 (append "/klava" to HEARTBEAT_URL) is dropped: the URL is used
  verbatim so dead-man receivers with exact ping URLs (healthchecks.io)
  work out of the box. client_name travels in the payload instead.
"""

from telegram_bot.core.health.healthcheck import run_healthcheck_loop
from telegram_bot.core.health.heartbeat import (
    build_payload,
    run_heartbeat_loop,
    send_shutdown_heartbeat,
)
from telegram_bot.core.health.state import HealthState

__all__ = [
    "HealthState",
    "build_payload",
    "run_healthcheck_loop",
    "run_heartbeat_loop",
    "send_shutdown_heartbeat",
]
