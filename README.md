# SdrScripts

Lifecycle management scripts for the SDR stack.

## Repos this controls

| Repo | What it does |
|---|---|
| SdrResourceManager | Controller — accepts tasks, schedules hardware, streams IQ |
| AcquisitionApp | Collects IQ from controller, stores detections in PostgreSQL |
| AnalysisApp | Receives IQ, identifies signal types, publishes results |
| SdrScanner | Qt6 GUI — live band scanner, displays detected signals |

## Two deployment modes

### Container mode (`sdr.sh`)
Everything runs in containers. Good for development and quick testing.

```bash
./sdr.sh start                     # full stack
./sdr.sh start broker controller   # core only
./sdr.sh stop
./sdr.sh status
./sdr.sh logs controller
./sdr.sh scanner                   # Qt GUI (needs DISPLAY)
./sdr.sh build controller          # rebuild an image
./sdr.sh test                      # run hardware test suite
```

### Native mode (`deploy.sh`)
Artemis and SoapySDRServer run in containers; controller/acquisition/analysis
install as native CentOS 10 RPMs and run as systemd services.

```bash
./deploy.sh build          # build RPMs (takes ~10 min)
./deploy.sh install        # dnf install + enable services  [sudo]
./deploy.sh start          # start containers + systemctl start
./deploy.sh stop
./deploy.sh status
./deploy.sh logs controller
./deploy.sh uninstall      # remove RPMs  [sudo]
```

First-time native setup:
```bash
./deploy.sh build install start
```

## Hardware

- PlutoSDR at `192.168.1.253` (override with `PLUTO_IP=x.x.x.x ./sdr.sh start`)
- Artemis broker: `amqp://localhost:5672`, user `sdr_ctrl` / `sdr_hw_test`

## Config XML overrides

Drop overrides in `SdrScripts/` and they take priority over `hw-test/` defaults:

```
SdrScripts/
├── devices.xml        # controller device config
├── scanner.xml        # acquisition scan params
└── analysis.xml       # analysis service config
```

## RPMs

Built RPMs land in `rpms/` (git-ignored). Rebuild with `./deploy.sh build`.
Targets CentOS Stream 10 (el10) x86_64.
