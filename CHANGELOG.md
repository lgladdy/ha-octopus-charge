# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The `pydantic` requirement now has a floor (`>=2`), since the models use the
  pydantic 2 API (suggested in the HACS default list review).

## [1.0.1] - 2026-11-09

### Removed

- Setup no longer offers to copy the API key from an Octopus Energy integration
  account. One integration shouldn't read another's stored credentials, so you
  enter your own API key and account number (the same key works for both).

## [1.0.0] - 2026-11-06

### Added

- Initial release as a standalone integration, split out from the charge point
  support developed for the Octopus Energy integration: live power, energy,
  boost, schedule, control mode, LED brightness, eco mode, random delay, away
  mode, cable auto lock, connectivity and status sensors.
- Setup can reuse the API key of an existing Octopus Energy integration account.
- Re-authentication flow when the API key is rejected.
- Diagnostics with account and device identifiers redacted.

### Changed

- Entities keep their last known values for up to 15 minutes when Octopus's API
  is failing (it often returns "Internal server error" for a few minutes at a
  time), instead of going unavailable on the first failed poll. Polling carries
  on at the normal rate meanwhile, so recovery is noticed within a minute.
- Failed requests are now logged with more context: HTTP status, how long the
  request took, the GraphQL error paths and extension fields, whether Octopus
  sent data alongside the errors, and identifying response headers (request
  ids, which layer answered). The first failed poll of an outage logs it, the
  recovery notes how long it lasted, and the diagnostics include the last
  failure. Failed commands and live power stream drops log the same details.
  Credentials and response data are never included; the body of a failed
  response is only logged with debug logging enabled.
- The schedule poll rides out short outages the same way, so a brief timeout no
  longer logs an error or delays the next attempt.
