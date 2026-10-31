# Octopus Charge

A Home Assistant custom integration for the [Octopus Charge](https://octopus.energy/octopus-charge/) home EV charger. It monitors and controls your charger straight from Home Assistant: live power, a cumulative energy total for the Energy dashboard, boost charging, the charging schedule and the charger's settings.

> **Status:** early development. Install from `main` for now; there are no tagged releases yet.

This code started life as charge point support for [BottlecapDave's Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy), and shares its heritage (and MIT licence) with it. It runs **alongside** that integration, rather than replacing it: keep using it for rates, consumption, Intelligent Octopus Go and so on.

## Features

- ⚡ **Live power** – instantaneous draw in kW, streamed while the charger is charging
- 🔋 **Energy** – a cumulative kWh total, ready for the Energy dashboard
- 🚀 **Boost** – start/stop a boost charge, or boost for a custom duration with a service
- 📅 **Schedule** – your weekly charging schedule as a sensor
- 🎛️ **Controls** – control mode, LED brightness, eco mode, random delay, away mode and cable auto lock
- 🔌 **Status** – operational state, charging method and connectivity

## Installation

### HACS (custom repository)

1. In HACS, open the three dots menu and choose **Custom repositories**
2. Add `https://github.com/lgladdy/ha-octopus-charge` with type **Integration**
3. Find **Octopus Charge**, click **Download** (choose the `main` branch while there are no releases) and restart Home Assistant

### Manual

Copy `custom_components/octopus_charge` into your Home Assistant `custom_components` directory and restart.

## Setup

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for **Octopus Charge**
3. Enter your Octopus Energy **API key** (from your [developer settings](https://octopus.energy/dashboard/new/accounts/personal-details/api-access)) and your **account number** (e.g. `A-1234ABCD`)

Your account must have an Octopus Charge unit registered against it. If your API key stops working you'll be asked to enter a new one.

## Entities

Everything for a charger is grouped under one **Octopus Charge** device. Entity IDs look like `sensor.octopus_charge_live_power`.

| Entity | Type | Description |
|--------|------|-------------|
| Operational state | sensor | What the charger is doing: `CHARGING`, `BOOST_CHARGING`, `NOT_CHARGING`, `UNPLUGGED`, `ONBOARDING` or `SMART_CONTROL_SCHEDULED` |
| Charging method | sensor | How charging is controlled: `SCHEDULED` or `ON_DEMAND` |
| Boost end time | sensor | When the current boost ends (unknown outside a boost) |
| Live power | sensor | Instantaneous power in kW, `0` when not charging |
| Energy | sensor | Cumulative energy in kWh (see below) |
| Schedule | sensor | Summary of the weekly schedule; the full schedule is in the `schedule` attribute |
| Connected | binary sensor | Whether the charger is online |
| Control mode | select | `SMART` or `MANUAL` |
| LED brightness | number | Status LED brightness, 0–100% |
| Eco mode | switch | Turn eco mode on/off |
| Random delay | switch | A small random delay before charging starts, to help smooth grid demand |
| Away mode | switch | Turn away mode on/off |
| Cable auto lock | switch | Only created if your charger supports it |
| Boost | switch | Start (for 1 hour) or stop a boost charge |

### Live power

Octopus provide this as a live stream rather than something that can be polled, so the integration only holds a connection open while the charger is `CHARGING` or `BOOST_CHARGING`. The connection is opened and closed when the operational state sensor (refreshed every minute) notices the change, so allow up to a minute of lag.

### Energy

Octopus's API has no live cumulative energy figure for the charger (only a total per finished session), so this sensor integrates the live power readings over time, the same approach as Home Assistant's "Integration - Riemann sum" helper. Two things to be aware of:

- It starts from 0 when first created, so it only covers consumption from then on.
- The running total is saved periodically and on shutdown. A restart in the middle of a charge loses whatever was drawn since the last save.

Add it to the Energy dashboard as an individual device.

### Schedule

The `schedule` attribute maps each day (`MONDAY` to `SUNDAY`) to a list of periods, each with `start` and `end` (`HH:MM`) and an `action` (e.g. `ON`). Days without a period are an empty list. The schedule is read only; change it in the Octopus app. A scheduled start or end also triggers a short burst of faster polling, so the state follows along quickly.

## Services

### `octopus_charge.boost_charge_point`

Starts a boost charge for a custom duration on one or more Boost switches.

```yaml
action: octopus_charge.boost_charge_point
target:
  entity_id: switch.octopus_charge_boost
data:
  hours: 2
  minutes: 30
```

Starting or stopping a boost polls every 10 seconds for a minute, so the state reflects the change quickly.

## Using it alongside Octopus Energy

The two integrations are independent and can both be set up with the same API key. Octopus Charge creates its own device, so you will no longer see charger entities attached to an Intelligent Octopus Go device.

If you previously used the charge point support from the fork of the Octopus Energy integration, adding Octopus Charge hands those entities over to this integration, keeping their history and your energy total, and renames them to the `octopus_charge` prefix. Update any automations or dashboards that referenced the old IDs. (This one-off migration is temporary and will be removed before the first release.)

## Troubleshooting

Download diagnostics from the integration's page to see the data returned by Octopus (account and device identifiers are redacted). Enable debug logging with:

```yaml
logger:
  logs:
    custom_components.octopus_charge: debug
```

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt ruff
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## Credits

Built from the charge point support written for [BottlecapDave's Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy), originally by David Kendall. Not affiliated with or endorsed by Octopus Energy.

## Licence

[MIT](LICENSE)
