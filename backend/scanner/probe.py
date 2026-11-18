"""Connection test for the PumpPortal feed, to run once on the operator's machine.

    python -m backend.scanner.probe --seconds 60

Connects with the configured API key, listens without touching the database, and prints what
was received: message counts per type, parse failures, the field names seen for each type, and
any server control messages. It validates the message format assumed by the parser.
"""

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from typing import Any

from backend.config import get_settings
from backend.monitoring.logging import configure_logging
from backend.scanner.messages import MessageError, event_type_of, parse_message
from backend.scanner.providers.base import ConnectionStatus, RawMessage
from backend.scanner.providers.pumpportal import PumpPortalProvider


async def probe(seconds: float, track_first: int) -> dict[str, Any]:
    settings = get_settings()
    key = settings.pumpportal_api_key.get_secret_value() if settings.pumpportal_api_key else None
    provider = PumpPortalProvider(settings.pumpportal_ws_url, key)
    counts: Counter[str] = Counter()
    fields: defaultdict[str, set[str]] = defaultdict(set)
    controls: list[str] = []
    connections: list[str] = []
    tracked = 0

    async def listen() -> None:
        nonlocal tracked
        async for item in provider.items():
            if isinstance(item, ConnectionStatus):
                connections.append("connected" if item.connected else f"lost:{item.reason}")
                continue
            if not isinstance(item, RawMessage):
                continue
            try:
                event = parse_message(item.payload)
                kind = event_type_of(event)
            except MessageError:
                kind = "malformed"
            counts[kind] += 1
            fields[kind].update(item.payload)
            if kind == "control":
                controls.append(str(item.payload)[:300])
            mint = item.payload.get("mint")
            if kind == "token_created" and tracked < track_first and isinstance(mint, str):
                await provider.track([mint])
                tracked += 1

    with_timeout = asyncio.wait_for(listen(), timeout=seconds)
    try:
        await with_timeout
    except TimeoutError:
        pass
    finally:
        await provider.close()
    return {
        "seconds": seconds,
        "connections": connections,
        "messages_by_type": dict(counts),
        "fields_by_type": {k: sorted(v) for k, v in fields.items()},
        "control_messages": controls[:10],
        "tokens_tracked_for_trades": tracked,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PumpPortal feed connection test")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--track", type=int, default=5, help="new tokens to follow for trades")
    args = parser.parse_args()
    settings = get_settings()
    configure_logging("WARNING", "console", settings.secret_values())
    report = asyncio.run(probe(args.seconds, args.track))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
