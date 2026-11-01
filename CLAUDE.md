# Octopus Charge Integration

A Home Assistant custom integration for the Octopus Charge home EV charger
("charge point"), split out of the charge point support written for
BottlecapDave's Octopus Energy integration. Domain: `octopus_charge`.

## Project Structure

```
custom_components/octopus_charge/
├── api/
│   ├── client.py            # Kraken auth (token cooldown), GraphQL, power stream
│   ├── exceptions.py        # Server / connection / request / auth errors
│   ├── models.py            # pydantic models mirroring the GraphQL schema
│   └── stream.py            # Parser for the multipart live power stream
├── utils/                   # energy integration, schedule transitions, backoff
├── brand/                   # icon.png / icon@2x.png
├── __init__.py              # Setup; stores runtime data in entry.runtime_data
├── config_flow.py           # API key + account number, reauth
├── coordinator.py           # Status coordinator (1 min, burst refresh) + schedule coordinator
├── entity.py                # Base entity, optimistic values, dynamic entity adding
├── sensor.py binary_sensor.py switch.py select.py number.py
└── diagnostics.py  services.yaml  icons.json  strings.json  translations/en.json
```

Root: `hacs.json`, `.github/workflows/` (lint, tests, hassfest, HACS),
`tests/`, `pyproject.toml`, `requirements_test.txt`, `CHANGELOG.md`.

## Things worth knowing

- **Refresh rate is deliberately 1 minute**, matching what the Octopus app does.
  Don't "fix" it to a slower interval.
- Two integrations may share one API key, and Octopus rate limits token
  retrieval. `api/client.py` has a cooldown on token retrieval failures; keep it.
- The status coordinator's burst refresh (10s for 60s after boost toggles or a
  schedule boundary) is the only thing polling during a burst - it deliberately
  does not touch `update_interval`.
- Live power is push driven (a long-lived multipart POST), only open while the
  charger is charging. The energy sensor integrates it and resolves the live
  power entity through the entity registry by unique id.
- A failing status poll is tolerated for `OUTAGE_TOLERANCE` (15 minutes): the
  coordinator returns its last data (polling at the normal rate, not backing
  off) and only raises `UpdateFailed` once the outage outlasts that. Octopus's
  API fails like this for a few minutes about once an hour.
- API errors carry a `details` dict (status, timing, error paths/extensions,
  request-id style headers). It is logged on the first failed poll of an outage
  and kept in `coordinator.last_failure` for diagnostics. It must never hold
  credentials or anything from the response `data` (serials, pins, etc.): add
  headers by name to `DIAGNOSTIC_HEADERS`, never dump a whole response. The
  body of a failed request is only logged at DEBUG (never put in `details`),
  and never for a successful token response, whose body is a credential.
- `strings.json` and `translations/en.json` must stay identical.
- Entity unique ids are `{device_uuid}_{key}` and entity ids come from the device
  name + translated entity name, e.g. `sensor.octopus_charge_live_power`.

## Testing

```bash
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Uses `pytest-homeassistant-custom-component`. Freeze time (`freezer`) *before*
setting the integration up - freezing afterwards makes every timer instantly due.
CI also runs against the minimum supported HA version (see `hacs.json` and the
`minimum` matrix entry in `.github/workflows/tests.yml`; keep them in step).
