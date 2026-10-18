# Brewski

Brewski is a local web controller for a small still. A Python program runs the heater logic and stores every run. A plain JavaScript dashboard, in a dark layout, shows live temperatures, heater state, and the history of those runs.

There is no Raspberry Pi wiring in this version. Sensors and heaters come from a **dummy source** that simulates a still, so the dashboard can be built before the hardware exists. A later hardware source can replace that dummy without changing the UI.

## What a run does

A run is one session of the still.

- **First launch.** A new controller, and one that has just had all data reset, opens a setup overlay. It asks for the heaters, the probes, the panel pins, the SPI and I2C bus, the target temperature, and the still size in gallons. A normal run stays locked until a calibration has measured the coast.
- **Idle.** The probes still report temperature. Heaters are off. Nothing new is written to the log unless the previous run is still cooling down.
- **Start.** Brewski copies the current setup onto the run: target temperature, which heaters and probes are enabled, their names, and their GPIO pins. The clock starts at zero. Starting during a cooldown leaves that cooldown unfinished, and it is left out of the average. Start is refused until a calibration has been saved.
- **Running.** About once a second the controller reads the probes. **The first enabled probe is the only one that controls the heaters.** Enabled heaters turn off early, by the early cutoff (3°F unless changed in Settings), so heat still stored in the element can carry the liquid the rest of the way to the target. They turn back on when the probe falls 2°F below that cutoff. Between those two points they stay as they are. A heater that has just turned on stays on for at least the minimum on time, and one that has just turned off stays off for at least the minimum off time (each 1 second unless changed). Both dwells keep a zero-crossing SSR from chattering. Pause and Stop still turn the heaters off immediately. Disabled heaters stay off. Every 5 seconds a sample is saved: each probe temperature, each heater on or off, and the target at that moment.
- **Pause.** Heaters are forced off and the run clock stops. Live temperatures are still shown. Paused time is not added to the log.
- **Stop.** Heaters turn off, the run is closed as **Stopped**, and the setup snapshot plus the sample log stay in the history.
- **Calibrate.** From the dashboard, with the pot filled with water, Calibrate heats until the control probe is a little above the target. The early cutoff is not used, so the still can actually reach the setpoint and coast past it. Once the element has been hot for 30 seconds and turns off, Brewski watches a rolling average of that probe. The run ends on its own when the average stops changing. The degrees it climbed after the heaters turned off, and how long that took, become the early cutoff. The calibration uses the still size from setup. A different size on a later calibration scales the remembered coast, then the measurement replaces it. Calibration runs stay in the log with their own badge and are left out of the run count, average duration, average cooldown, and the per-run heater-on totals. They still add to each element's hour meter, because the element was powered.
- **Cooldown.** A cooldown is the stretch from the target temperature to the lower stop. Timing starts the first time the control probe reaches the target, including the rest of the run. After Stop, heaters stay off, and the lower stop is when that probe stays inside a 2°F band for 5 minutes. Those readings stay on the dashboard chart, and the time is saved. The dashboard and the history page show the average of every finished cooldown. A run that never reaches its target has no cooldown.

Each heater and each probe can be enabled or disabled on its own. Changing names, which devices are enabled, a heater's GPIO pin, a probe's MAX31865 chip select, RTD, wire count, or reference resistor, or any other GPIO pin, is locked while a run is open. The target temperature, the early cutoff, and the minimum on and off times can still be changed; later samples record the new target, and the run keeps the target it started with.

Any enabled probe after the control probe is logged and charted only. It does not decide when the heaters fire.

## Dummy source

`DummySource` stands in for the pot and the heaters. Two heaters together lift the boiler from about 70°F toward a 165°F target in a few minutes. That is faster than a real still, on purpose, so the chart moves while the UI is being designed. After the heaters turn off, stored element heat keeps raising the boiler for a few seconds, then it cools back toward room temperature. Sensor 2 lags sensor 1 and sits about 8°F cooler.

The status payload includes `"source": "dummy"`. The dashboard shows that as a badge. Each heater stores a BCM GPIO pin, and each probe stores the chip select of its MAX31865, so a later Raspberry Pi source can be wired differently on each still. Defaults are BCM 12 and 16 for the heaters, and chip selects BCM 17 and 8 for the probes. Probe 2 starts disabled. Both converters share SPI (defaults BCM 11 CLK, 9 SDO, and 10 SDI). Settings also store the I2C bus (BCM 2 SDA and 3 SCL), enable switches for the HTS221 and the LCD, the encoder (BCM 27 and 22), the start and stop LED relays (BCM 23 and 24), and the start and stop buttons (BCM 5 and 6). Every one of those pins must be unique. This version does not import GPIO or SPI, and the dummy source ignores the pin numbers. A future Raspberry Pi source should use the same two methods:

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

Git is not required. On the Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/anomaddev/Brewski/main/deploy/install.sh | sudo bash
```

That installs the latest GitHub release, then enables a systemd service so the controller starts on every reboot. Each later boot installs a newer release before the controller starts. Commits on `main` stay off the Pi until they are published as a release. The database is kept. If GitHub cannot be reached, or there is no release yet, the version already on the Pi starts. The service listens on port 8080. The database is `/var/lib/brewski/brewski.db`. Open `http://<pi-address>:8080`.

The service runs as the account that invoked `sudo`, or as `pi` when that account exists and sudo did not record a user. Set `BREWSKI_USER` to choose a different account:

```bash
curl -fsSL https://raw.githubusercontent.com/anomaddev/Brewski/main/deploy/install.sh \
  | sudo BREWSKI_USER=still bash
```

From a checkout that is already on the Pi, `sudo ./deploy/install.sh` installs that checkout and does not contact GitHub.

Running the script again upgrades Brewski and restarts the service. It does not delete the database. Dev mode (`brewski dev`) does not need this install.

## What is stored

SQLite, one file. There is no separate database server.

**Settings** (one row): the target in °F, and for each of the two heaters and two probes a name, an enabled switch, and a BCM pin. A heater pin drives its SSR. A probe pin is the chip select of that probe's MAX31865. The same row stores the rest of the BCM map: SPI CLK, SDO, and SDI; I2C SDA and SCL; enable switches for the HTS221 and the LCD on that bus; encoder A and B; the start and stop LED relays; and the start and stop buttons. Every pin in that map, including both heaters and both chip selects, must be unique and from BCM 2 to 27. Each probe also stores its RTD (`pt100` or `pt1000`), wire count (2, 3, or 4), and reference resistor in ohms. Disabling a device keeps its name and wiring. Defaults are BCM 12 and 16 for the heaters and chip selects BCM 17 and 8 for the probes. Probe 2 starts disabled. SPI defaults are BCM 11, 9, and 10. I2C defaults are BCM 2 and 3, with the HTS221 and the LCD enabled. The encoder defaults are BCM 27 and 22, the LED relays BCM 23 and 24, and the buttons BCM 5 and 6. Each probe starts as a 3-wire PT100 with a 430 Ω reference. The allowed target range is 40–250°F. I2C addresses are not stored yet.

**Runs:** start time, end time, status (`running`, `paused`, or `stopped`), elapsed seconds (the pause does not add time), and a snapshot of the settings from the moment Start was pressed.

**Samples:** time, elapsed seconds, temperatures, heater states, and the target that was active when the sample was written.

Time each heater spent on during a run is estimated from that log and shown on the history page. A sample's heater state is treated as holding until the next sample.

**Element hours:** seconds each heater output has actually been on, including calibration and every run. The controller adds that time as it happens and keeps it when run data is reset. The dashboard, the history overview, and Settings show the total in hours. Replacing an element in Settings zeros that heater so the new one starts at zero. A database from before this meter existed is filled once from its sample log.

## Layout

```
src/brewski/          Python controller, API, and CLI
src/brewski/sources/  Hardware contract and the dummy still
web/                  Vite dashboard (plain JavaScript, no TypeScript)
deploy/               Pi install script and systemd unit
tests/
```

`brewski` with no arguments serves the API and, when it has been built, the dashboard. `brewski dev` is the Mac development command described above.
