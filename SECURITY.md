# Security Policy

## Supported versions

This is a community-maintained Home Assistant custom integration. Only the
latest version receives fixes. Please make sure you can reproduce an issue on
the current version before reporting it.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately using GitHub's
[private vulnerability reporting](https://github.com/lgladdy/ha-octopus-charge/security/advisories/new)
(the "Report a vulnerability" button under the repository's **Security** tab).

Please include:

- the integration version and your Home Assistant version
- a description of the issue and its impact
- steps to reproduce, if possible

You can expect an initial response within about a week. Once a fix is available
it will be released and the advisory published, crediting you unless you ask
otherwise.

## Scope

This integration talks to Octopus Energy's APIs using your personal API key. The
API key and your account number are stored in the Home Assistant config entry,
and the key is redacted from diagnostics downloads. The integration only reads
and controls your Octopus Charge unit; it does not handle payment data.

Vulnerabilities in Home Assistant core, HACS, or Octopus Energy's APIs should be
reported to those projects directly.
