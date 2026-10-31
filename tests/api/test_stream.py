"""Tests for the live power stream parser."""

from __future__ import annotations

from custom_components.octopus_charge.api.models import ChargePointPowerReading
from custom_components.octopus_charge.api.stream import parse_power_stream_chunk


def make_part(body: str) -> str:
    return f"Content-Type: application/json\n\n{body}\n"


def reading_body(value: float) -> str:
    return (
        '{"payload":{"data":{"electricChargerPowerReadings":'
        f'{{"value":{value},"unit":"KILOWATT"}}}}}}}}'
    )


def kw(value: float) -> ChargePointPowerReading:
    return ChargePointPowerReading(value=value, unit="KILOWATT")


def test_single_complete_reading() -> None:
    chunk = f"--graphql\n{make_part(reading_body(3.2))}--graphql\n".encode()

    _, readings = parse_power_stream_chunk("", chunk)

    assert readings == [kw(3.2)]


def test_heartbeat_yields_nothing() -> None:
    chunk = f"--graphql\n{make_part('{}')}--graphql\n".encode()

    _, readings = parse_power_stream_chunk("", chunk)

    assert readings == []


def test_null_reading_yields_none() -> None:
    body = '{"payload":{"data":{"electricChargerPowerReadings":null}}}'
    chunk = f"--graphql\n{make_part(body)}--graphql\n".encode()

    _, readings = parse_power_stream_chunk("", chunk)

    assert readings == [None]


def test_malformed_reading_yields_none_without_crashing() -> None:
    body = '{"payload":{"data":{"electricChargerPowerReadings":{"unit":"KILOWATT"}}}}'
    chunk = f"--graphql\n{make_part(body)}--graphql\n".encode()

    _, readings = parse_power_stream_chunk("", chunk)

    assert readings == [None]


def test_reading_split_across_chunks_parses_once_boundary_arrives() -> None:
    full_part = f"--graphql\n{make_part(reading_body(1.5))}"
    split = len(full_part) // 2

    buffer, first = parse_power_stream_chunk("", full_part[:split].encode())
    _, second = parse_power_stream_chunk(
        buffer, (full_part[split:] + "--graphql\n").encode()
    )

    assert first == []
    assert second == [kw(1.5)]


def test_multiple_readings_in_one_chunk_parse_in_order() -> None:
    chunk = (
        f"--graphql\n{make_part(reading_body(1.0))}"
        f"--graphql\n{make_part('{}')}"
        f"--graphql\n{make_part(reading_body(2.0))}"
        "--graphql\n"
    ).encode()

    _, readings = parse_power_stream_chunk("", chunk)

    assert readings == [kw(1.0), kw(2.0)]


def test_closing_marker_is_ignored() -> None:
    _, readings = parse_power_stream_chunk("", b"--graphql--")

    assert readings == []


def test_malformed_json_is_skipped() -> None:
    chunk = f"--graphql\n{make_part('{not valid json')}--graphql\n".encode()

    _, readings = parse_power_stream_chunk("", chunk)

    assert readings == []


def test_crlf_line_endings() -> None:
    chunk = (
        "--graphql\r\nContent-Type: application/json\r\n\r\n"
        + reading_body(4.4)
        + "\r\n--graphql\r\n"
    ).encode()

    _, readings = parse_power_stream_chunk("", chunk)

    assert readings == [kw(4.4)]


def test_crlf_split_across_chunks() -> None:
    """A chunk ending between the \\r and \\n of a CRLF still parses."""
    message = (
        b"--graphql\r\ncontent-type: application/json\r\n\r\n"
        b'{"payload":{"data":{"electricChargerPowerReadings":'
        b'{"value":7.2,"unit":"kW"}}}}\r\n--graphql'
    )
    split = message.index(b"\r\n\r\n") + 3  # between the last \r and \n

    buffer, first = parse_power_stream_chunk("", message[:split])
    buffer, second = parse_power_stream_chunk(buffer, message[split:])

    assert first == []
    assert [reading.value for reading in second] == [7.2]


def test_error_parts_are_not_zero_readings() -> None:
    """An error carries no reading; it mustn't be reported as 0kW."""
    message = (
        b"--graphql\r\ncontent-type: application/json\r\n\r\n"
        b'{"payload":null,"errors":[{"message":"JWT has expired"}]}\r\n'
        b"--graphql\r\ncontent-type: application/json\r\n\r\n"
        b'{"payload":{"errors":[{"message":"boom"}]}}\r\n--graphql'
    )

    _, readings = parse_power_stream_chunk("", message)

    assert readings == []


def test_field_errors_with_a_null_reading_are_not_zero_readings() -> None:
    message = (
        b"--graphql\r\ncontent-type: application/json\r\n\r\n"
        b'{"payload":{"data":{"electricChargerPowerReadings":null},'
        b'"errors":[{"message":"boom"}]}}\r\n--graphql'
    )

    _, readings = parse_power_stream_chunk("", message)

    assert readings == []
