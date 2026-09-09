# piphi-network-airthings

PiPhi runtime integration for the Airthings Consumer Cloud API.

This integration targets consumer Airthings accounts and polls the latest cloud sample for configured devices using an Airthings API client id and client secret created from the Airthings dashboard integrations page.

## Current scope

- Discovers devices from an Airthings consumer account
- Configures one PiPhi runtime entity per Airthings serial number
- Polls `latest-samples` on a configurable interval
- Reports a fresh Core health heartbeat on every successful poll while preserving
  the Airthings device timestamp on measurement telemetry. Repeated cloud samples
  therefore prove the connection is healthy without making old readings look new.
- Emits telemetry and runtime events on success and failure
- Supports refresh commands, config sync, diagnostics, and health endpoints through the PiPhi Python runtime SDK

## API assumptions

The implementation is based on Airthings’ published consumer API onboarding plus the shared Airthings OAuth/device endpoint patterns:

- Consumer API access is created from the Airthings dashboard integrations page
- OAuth tokens are issued by `https://accounts-api.airthings.com/v1/token`
- Device data is requested from the consumer cloud API using `devices` and `latest-samples` endpoints

The consumer docs are not as indexable as the business docs, so the exact path conventions are implemented conservatively and kept configurable inside the cloud client if Airthings changes them.

## Local development

```bash
pdm install -G dev
pdm run uvicorn piphi_network_airthings.app:app --reload --port 3669
```

## Testing

```bash
pdm run pytest
```
