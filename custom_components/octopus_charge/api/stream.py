"""Parser for the live power multipart stream."""

from __future__ import annotations

import contextlib
import json
import logging

from pydantic import ValidationError

from .models import ChargePointPowerReading

# The multipart boundary used by the charge point live power stream. A fixed
# literal, never guaranteed to land on a chunk edge.
MULTIPART_BOUNDARY = "--graphql"

_LOGGER = logging.getLogger(__name__)


def parse_power_stream_chunk(
    buffer: str, chunk: bytes
) -> tuple[str, list[ChargePointPowerReading | None]]:
    """Feed one more chunk of bytes from the live power stream into the parser.

    Returns a tuple of (new_buffer, readings) where readings is a list of parsed
    values - each either a ChargePointPowerReading (a real reading) or None (the
    stream explicitly reported no reading for that update). Empty ``{}``
    heartbeat payloads and partial/malformed chunks are swallowed and contribute
    nothing to the returned readings list.

    This is a pure function so the buffering/boundary/JSON-extraction logic can
    be unit tested without a real (or mocked) HTTP connection.
    """
    readings: list[ChargePointPowerReading | None] = []

    # Normalise CRLF up front so the boundary/header-body parsing below only
    # ever has to deal with plain \n. Done on the joined buffer, not the chunk,
    # because a chunk can end between the \r and the \n.
    buffer = (buffer + chunk.decode("utf-8", errors="ignore")).replace("\r\n", "\n")

    # Buffer across reads and only treat a segment as complete once a boundary
    # after it has arrived.
    parts = buffer.split(MULTIPART_BOUNDARY)
    buffer = parts.pop()  # last piece may be incomplete - carry it forward

    for raw_part in parts:
        part = raw_part.strip()
        # Empty (the bit before the very first boundary) or the closing
        # "--graphql--" marker (left as bare "--" once already split on
        # "--graphql") - nothing to parse either way.
        if not part or part == "--":
            continue

        body_start = part.find("\n\n")
        body_text = (part[body_start + 2 :] if body_start >= 0 else part).strip()
        if not body_text:
            continue

        try:
            payload_data = json.loads(body_text)
        except ValueError:
            continue  # partial/malformed chunk - skip rather than crash the stream

        if not payload_data:
            continue  # {} heartbeat

        raw_reading = None
        payload = (
            payload_data.get("payload") if isinstance(payload_data, dict) else None
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("data"), dict)
            or payload_data.get("errors")
            or payload.get("errors")
        ):
            # An error part (transport-, payload- or field-level, the last with a
            # null reading alongside it) carries no reading - it isn't "no
            # power", so don't report it as 0kW.
            _LOGGER.debug("Ignoring live power stream part without data: %s", body_text)
            continue

        raw_reading = payload["data"].get("electricChargerPowerReadings")

        reading = None
        if raw_reading is not None:
            # A reading present but missing/malformed fields (e.g. no "value")
            # is treated like "no reading" rather than crashing the whole stream.
            with contextlib.suppress(ValidationError):
                reading = ChargePointPowerReading.model_validate(raw_reading)

        readings.append(reading)

    return buffer, readings
