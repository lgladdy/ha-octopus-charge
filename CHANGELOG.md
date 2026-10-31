# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial release as a standalone integration, split out from the charge point
  support developed for the Octopus Energy integration: live power, energy,
  boost, schedule, control mode, LED brightness, eco mode, random delay, away
  mode, cable auto lock, connectivity and status sensors.
- Setup can reuse the API key of an existing Octopus Energy integration account.
- Re-authentication flow when the API key is rejected.
- Diagnostics with account and device identifiers redacted.
