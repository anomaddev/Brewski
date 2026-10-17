# Brewski

Brewski is a local web controller for a small still. A Python program runs the heater logic and stores every run. A plain JavaScript dashboard, in a dark layout, shows live temperatures, heater state, and the history of those runs.

There is no Raspberry Pi wiring in this version. Sensors and heaters come from a **dummy source** that simulates a still, so the dashboard can be built before the hardware exists. A later hardware source can replace that dummy without changing the UI.

## What a run does

A run is one session of the still.

- **Idle.** The probes still report temperature. Heaters are off. Nothing new is written to the log unless the previous run is still cooling down.
- **Start.** Brewski copies the current setup onto the run: target temperature, which heaters and probes are enabled, their names, and their GPIO pins. The clock starts at zero. Starting during a cooldown leaves that cooldown unfinished, and it is left out of the average.
- **Running.** About once a second the controller reads the probes. **The first enabled probe is the only one that controls the heaters.** Enabled heaters turn on when that probe is 2°F below the target and turn off when it reaches the target. Between those two points they stay as they are, so they do not chatter. Disabled heaters stay off. Every 5 seconds a sample is saved: each probe temperature, each heater on or off, and the target at that moment.
- **Pause.** Heaters are forced off and the run clock stops. Live temperatures are still shown. Paused time is not added to the log.
- **Stop.** Heaters turn off, the run is closed as **Stopped**, and the setup snapshot plus the sample log stay in the history.
- **Cooldown.** A cooldown is the stretch from the target temperature to the lower stop. Timing starts the first time the control probe reaches the target, including the rest of the run. After Stop, heaters stay off, and the lower stop is when that probe stays inside a 2°F band for 5 minutes. Those readings stay on the dashboard chart, and the time is saved. The dashboard and the history page show the average of every finished cooldown. A run that never reaches its target has no cooldown.

Each heater and each probe can be enabled or disabled on its own. Changing names, which devices are enabled, or their GPIO pins is locked while a run is open. The target temperature can still be changed; later samples record the new target, and the run keeps the target it started with.

Any enabled probe after the control probe is logged and charted only. It does not decide when the heaters fire.

## Dummy source

`DummySource` stands in for the pot and the heaters. Two heaters together lift the boiler from about 70°F toward a 165°F target in a few minutes. That is faster than a real still, on purpose, so the chart moves while the UI is being designed. With the heaters off, the boiler cools back toward room temperature. Sensor 2 lags sensor 1 and sits about 8°F cooler.

The status payload includes `"source": "dummy"`. The dashboard shows that as a badge. Each heater and probe stores a BCM GPIO pin so a later Raspberry Pi source can be wired differently on each still. This version does not import GPIO, and the dummy source ignores the pin numbers. A future Raspberry Pi source should use the same two methods:

- `read_temperatures(count)` returns `count` temperatures in °F, sensor 1 first.
- `set_heaters(states)` turns each heater on or off, heater 1 first.

Those methods live in `src/brewski/sources/base.py`. The dummy implementation is `src/brewski/sources/dummy.py`. Nothing in this project imports GPIO.

## Develop on a Mac

From a checkout of this repo:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
npm install --prefix web
brewski dev
```

- API: http://127.0.0.1:5000
- Dashboard: http://127.0.0.1:5173

The Vite dev server reloads the UI as you edit `web/`, and it proxies `/api` to the Python process. The database for dev mode is `./data/brewski.db`.

`BREWSKI_HOST`, `BREWSKI_PORT`, and `BREWSKI_DATA_DIR` override the API bind address and the database directory. `BREWSKI_DEV_PORT` changes the Vite port (default 5173).

Run the tests with `pytest`.

## Install on a Raspberry Pi

Build the dashboard on a machine that has Node, then install the Python program. The Pi does not need Node just to run Brewski.

On the build machine, from the repo:

```bash
npm install --prefix web
npm run build --prefix web
```

That writes `web/dist`. Copy the repo to the Pi (or build there if Node is installed) and install it into a virtualenv:

```bash
sudo mkdir -p /opt/brewski /var/lib/brewski
sudo chown pi:pi /opt/brewski /var/lib/brewski
python3 -m venv /opt/brewski/venv
/opt/brewski/venv/bin/pip install /path/to/Brewski
```

`pip install` packages the built dashboard into Brewski, so `brewski` serves both the API and the UI. The service listens on port 8080.

```bash
sudo cp deploy/brewski.service /etc/systemd/system/brewski.service
sudo systemctl daemon-reload
sudo systemctl enable --now brewski
```

Edit `User=` and `Group=` in the unit if the Pi account is not `pi`. The database is `/var/lib/brewski/brewski.db`. Open `http://<pi-address>:8080`.

If `web/dist` was missing at install time, the API still runs and the UI routes explain how to build it. Dev mode (`brewski dev`) does not need that build.

## What is stored

SQLite, one file. There is no separate database server.

**Settings** (one row): the target in °F, and for each of the two heaters and two probes a name, an enabled switch, and a BCM GPIO pin. Disabling a device keeps its name and pin. Heater pins must be unique and cannot match a probe pin. Probes may share a pin, which is how a 1-wire bus is wired. Defaults are BCM 17 and 27 for the heaters and BCM 4 for both probes. The allowed target range is 40–250°F.

**Runs:** start time, end time, status (`running`, `paused`, or `stopped`), elapsed seconds (the pause does not add time), and a snapshot of the settings from the moment Start was pressed.

**Samples:** time, elapsed seconds, temperatures, heater states, and the target that was active when the sample was written.

Time each heater spent on is estimated from that log and shown on the history page. A sample's heater state is treated as holding until the next sample.

## Layout

```
src/brewski/          Python controller, API, and CLI
src/brewski/sources/  Hardware contract and the dummy still
web/                  Vite dashboard (plain JavaScript, no TypeScript)
deploy/brewski.service
tests/
```

`brewski` with no arguments serves the API and, when it has been built, the dashboard. `brewski dev` is the Mac development command described above.
