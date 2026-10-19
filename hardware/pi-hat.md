# Brewski Pi hat

This is the spec for the first Brewski breakout. It plugs onto the Raspberry Pi's 40-pin header and gives every external part a connector. The next step is to hand this file to the board-design software and have it draw the schematic from the nets and parts below. Layout and fabrication come after that schematic is reviewed.

The board follows the Raspberry Pi HAT mounting holes and header position, so it bolts onto the Pi. It is larger than the 65 × 56.5 mm HAT outline. The lamp section needs the extra room. This revision has no ID EEPROM. Brewski talks to BCM pins directly and does not read a HAT EEPROM. GPIO 0 and GPIO 1 stay unused.

Pin numbers below are the defaults in `src/brewski/store.py`. Setup can change them in software. The hat is wired to these defaults, and a still that has already saved other pins would need those settings put back, or a later board revision.

## What each part does

Brewski is the controller for a small still. The Pi runs the heater logic. These are the parts that logic is wired to.

**Heater SSRs.** Two SSR-40DA (also sold as ZGT-40DA) switch the heating elements. The control input is 3–32 V DC at 5–25 mA. The load side is 24–480 V AC at up to 40 A. Each SSR is optically isolated inside its own case, screwed to a heatsink, and mounted on a DIN rail in the cabinet. The hat does not carry the SSR. A pair of wires runs from a screw terminal on the hat to that SSR's control input. On a usual SSR-40DA body, terminals 3 and 4 are the DC control input (3 is +, 4 is −) and terminals 1 and 2 are the AC load. The marks on the relay in hand win over that numbering.

**Still probes.** One RTD PT100, read by a HiLetgo MAX31865 module, is the boiler temperature. The software already has a second probe (column). It is disabled until setup turns it on. Both modules are on hardware SPI0. Each has its own chip select. The module carries a 430 Ω reference resistor, which matches the software default (2-wire PT100, 430 Ω). The RTD wires land on the screw terminals that are already on the module.

**Cabinet sensor.** An Adafruit HTS221 on the I2C bus reads temperature and humidity inside the cabinet. Temperature is the safety interlock. Humidity is display only. It plugs into the hat with a STEMMA QT cable.

**LCD.** A 20×4 character display with an I2C backpack. Brewski probes address `0x27`, then `0x3F`. It shows the run, the menu, and emergency codes. It plugs into the hat with a STEMMA QT cable, on the same bus as the HTS221.

**I2C hub.** The hat carries the hub. Five STEMMA QT jacks share SDA, SCL, 3.3 V, and ground. The LCD takes one, the HTS221 takes one, and three are spare.

**Target dial.** An Adafruit CNC rotary encoder, 100 pulses per rotation, mounted on the front panel. Quadrature outputs A and B. One step changes the target by 1°F. A four-position screw terminal on the hat carries A, B, 3.3 V, and ground out to the dial.

**5 V supply.** A 5 V supply on the DIN rail powers the Pi and every 5 V load. One screw terminal on the hat is that input, and the hat distributes it.

**Start and stop buttons.** Two mxuteuk LA155-A1-11D momentary push buttons, 22 mm, one green and one red. Each has a 1NO and a 1NC contact, and a separate lamp rated 110–220 V with its own internal resistor. The contact and the lamp are independent. Brewski reads the normally open contact, wired to ground. A 10 kΩ resistor from 3.3 V to the GPIO holds the pin high, and the Pi's internal pull-up stays on. A press reads low. The normally closed contact is unused. Green is Start. Red is Stop.

**Button lamps, today.** A SunFounder 2-channel 5 V relay module (active low) switches those two lamps. The relay exists because the lamp is a mains-voltage indicator and a Pi pin cannot drive it. That module is removed. The hat replaces each relay channel with an optocoupler.

## What this board changes

Five wiring decisions define the hat.

1. **The mechanical relay module is gone.** Each button lamp is switched by its own zero-cross phototriac optocoupler. The Pi only drives the optocoupler's LED. The 110–220 V lamp circuit stays on the far side of the optocoupler.
2. **The heater SSRs stay on the DIN rail.** The hat does not carry heater current. Each SSR gets its own two-position screw terminal on the hat, and a control cable runs from that terminal to the SSR. Heater AC lands only on the SSR's own load screws.
3. **The I2C hub is on the hat.** Five STEMMA QT jacks replace the separate Adafruit hub. The LCD and the HTS221 each take one.
4. **The dial cable lands on a screw terminal.** Four positions: A, B, 3.3 V, and ground.
5. **5 V comes from the DIN rail.** One screw terminal is the power input. The hat feeds the Pi from it and distributes 5 V to every 5 V load. The Pi's own regulator then makes the 3.3 V rail.

The circuits below are drawn so the current software still matches, with no polarity change:

| Function | BCM | Header pin | On when the pin is | Where the software says so |
| --- | --- | --- | --- | --- |
| Heater 1 SSR | 12 | 32 | high | `active_high=True` in `sources/pi.py` |
| Heater 2 SSR | 16 | 36 | high | same |
| Start lamp | 24 | 18 | low | `LED_ACTIVE_HIGH = False` in `lcd.py` |
| Stop lamp | 23 | 16 | low | same |
| Start button | 5 | 29 | low (pressed to ground) | `pull_up=True` in `panel.py` |
| Stop button | 6 | 31 | low (pressed to ground) | same |
| Encoder A | 27 | 13 | quadrature | `RotaryEncoder` in `panel.py` |
| Encoder B | 22 | 15 | quadrature | same |
| SPI CLK | 11 | 23 | hardware SPI0 | `sources/max31865.py` |
| SPI SDO (MISO) | 9 | 21 | hardware SPI0 | same |
| SPI SDI (MOSI) | 10 | 19 | hardware SPI0 | same |
| Probe 1 chip select | 17 | 11 | low to select | probe GPIO in settings |
| Probe 2 chip select | 8 | 24 | low to select | probe GPIO in settings |
| I2C SDA | 2 | 3 | hardware I2C1 | LCD and HTS221 |
| I2C SCL | 3 | 5 | hardware I2C1 | same |

## Safety rules for the layout

The hat has two voltages that must not meet.

- **Logic side.** The 5 V input from the DIN rail, the Pi header, the SSR control terminals, the probe sockets, the STEMMA jacks, the encoder, and the button contacts.
- **Lamp side.** 110–220 V AC for the two indicator lamps only. It crosses onto the logic side through the two optocouplers and nowhere else.

Heater AC does not enter this board at any terminal.

Layout rules the design software should follow:

- Put the lamp supply terminal, the fuse, both optocouplers, and the lamp pins of the button terminals on one edge of the board, away from the 40-pin header.
- Use a DIP-6 optocoupler with at least 7 mm of creepage. Cut a slot in the board under each package, between the LED pins and the phototriac pins.
- Keep lamp copper on the lamp side of that slot. Logic copper stays on the header side.
- Silkscreen the power terminal `5V IN`, each SSR terminal `SSR CONTROL 5V`, and the lamp terminal `LAMP 110-220V`, so the three cannot be confused in the cabinet.
- One fuse, 100 mA, in the lamp hot feed. Each lamp draws only a few milliamps. The fuse is there for a wiring fault.

## Circuits

Reference designators in this section are the ones to use on the schematic.

### Heater control (screw terminals)

The SSRs are mounted on a DIN rail, so the control wiring is a cable, not a header on the hat. J2 and J3 are screw terminals for that cable: two positions each, one pair per SSR. The wire lands under the screw on the hat and under the screw on the SSR.

Each SSR input is driven from the hat's 5 V rail through a small NPN transistor, so the GPIO only has to supply base current. That rail is the DIN-rail supply, not a second regulator. A high on the GPIO turns that SSR on. A 10 kΩ pull-down holds the transistor off while the Pi is booting, before Brewski claims the pin. The software also starts the output off.

```
5V ---------------- SSR terminal +
GPIO -- 1k -- base of 2N3904
10k from the GPIO net to GND
emitter -- GND
collector -- SSR terminal -
```

The SSR's own input circuit limits the current (about 5–12 mA at 5 V). No flyback diode. The input is an LED, which is the same reason a bare GPIO is a poor driver: the pin would be spending its current budget on the SSR, and both heaters can be on together.

| Part | Heater 1 | Heater 2 |
| --- | --- | --- |
| Screw terminal | J2, label `SSR1 +` and `SSR1 -` | J3, label `SSR2 +` and `SSR2 -` |
| Transistor | Q1 2N3904 | Q2 2N3904 |
| Base resistor | R5 1 kΩ | R6 1 kΩ |
| Pull-down | R7 10 kΩ | R8 10 kΩ |
| GPIO | BCM 12 | BCM 16 |

Run a pair of wires from J2 to heater 1's SSR on the DIN rail: `SSR1 +` to the SSR's DC+ terminal and `SSR1 -` to DC−. Same from J3 to heater 2. Reversing a pair can damage that SSR's input. The AC load stays on the SSR's own screws, on the rail.

### Button contacts

Each button's normally open contact goes to its GPIO. The common goes to Pi ground. A 10 kΩ resistor from 3.3 V to that GPIO holds the pin high while the button is open. The Pi's internal pull-up stays on as well (`pull_up=True` in `panel.py`). A press still reads low: the contact shorts the GPIO to ground and the resistor draws about 0.33 mA.

The internal pull-up alone is about 50 kΩ. On the cable out to the panel that is too weak. Noise was holding the start pin low, so a press never showed up, and it was chattering the stop pin. The 10 kΩ is what keeps an open button high. Leave the internal pull-up enabled. It pulls the same way and still covers a board where the resistor is missing.

```
3.3V -- 10k -- GPIO
GPIO -------- button NO
button COM -- GND
```

There is no capacitor on the button. Contact bounce is handled in software, which ignores edges inside 50 ms (`BUTTON_BOUNCE_SECONDS` in `lcd.py`). Fit the 10 kΩ on both buttons.

The normally closed contact has a pin on the connector and no net past that pin. It is there so a later revision can use it without a new cable.

| Part | Start (green) | Stop (red) |
| --- | --- | --- |
| Terminal | J11 | J12 |
| NO | BCM 5 | BCM 6 |
| COM | GND | GND |
| NC | no net | no net |
| Pull-up | R11 10 kΩ, 3.3 V to BCM 5 | R12 10 kΩ, 3.3 V to BCM 6 |

### Button lamps (optocouplers)

The lamp inside each button is a 110–220 V indicator with an internal resistor. The two lamp terminals have no polarity. This is the load the mechanical relays were switching, and it is the load the optocouplers switch instead.

Use a **MOC3063** for each lamp (zero-cross phototriac, 600 V, 5 mA trigger). The lamp current is a few milliamps, which this phototriac can carry on its own. Its holding current is about 250 µA, so the lamp stays lit. A power triac is the wrong part here: its holding current is larger than the lamp current, and the lamp would flicker or never stay on.

The GPIO side is active low, matching the relay module and `LED_ACTIVE_HIGH = False`.

```
3.3V -- 220Ω -- MOC3063 pin 1 (anode)
MOC3063 pin 2 (cathode) -- GPIO
10k from that GPIO net to 3.3V
pin 3 and pin 5 -- no connect

Lamp hot -- F1 100mA -- button lamp terminal L1
button lamp terminal L2 -- MOC3063 pin 6
MOC3063 pin 4 -- lamp neutral
```

220 Ω sets the LED current near 9 mA from 3.3 V, above the 5 mA trigger rating and inside what a Pi pin can sink. The 10 kΩ pull-up keeps the lamp off until Brewski drives the pin low.

A snubber across each phototriac, 39 Ω in series with 0.01 µF, is the network from the MOC3063 application circuit. The lamp is resistive, so the snubber is optional. Put the footprints in (R9, C3 for Start; R10, C4 for Stop) and use a capacitor rated for the mains voltage.

| Part | Start lamp | Stop lamp |
| --- | --- | --- |
| Optocoupler | OK1 MOC3063 | OK2 MOC3063 |
| LED resistor | R1 220 Ω | R2 220 Ω |
| Pull-up | R3 10 kΩ to 3.3 V | R4 10 kΩ to 3.3 V |
| GPIO | BCM 24 | BCM 23 |
| Snubber | R9 39 Ω, C3 0.01 µF | R10 39 Ω, C4 0.01 µF |

J14 is the lamp supply screw terminal, labeled `LAMP L` and `LAMP N`. Both lamps share it, downstream of F1. J11 pins `L1` and `L2` are the Start lamp. J12 pins `L1` and `L2` are the Stop lamp.

If a later button uses a low-voltage LED instead of the 110–220 V lamp, the GPIO side of this circuit stays. Only the phototriac output changes, to a transistor-output optocoupler and a low-voltage LED supply. That is a later revision.

### Target dial (screw terminals)

The encoder is on the front panel, so the wiring is a cable. J13 is a four-position screw terminal for that cable: the two quadrature pins, power, and ground.

| Position | Label | Net |
| --- | --- | --- |
| 1 | `+3V3` | Pi 3.3 V |
| 2 | `GND` | GND |
| 3 | `A` | BCM 27 |
| 4 | `B` | BCM 22 |

The encoder's own screws are VCC, GND, A, and B. VCC is the pull-up supply for A and B, so the `+3V3` position is the one that lands on the encoder's VCC screw. 5 V on that wire would put 5 V on BCM 27 and BCM 22.

### 5 V input (screw terminals)

A 5 V supply on the DIN rail is the only power source. J15 is the screw terminal for it: `+5V` and `GND`. The hat distributes that input.

```
DIN +5V -- J15 +5V -- F2 -- 5V rail -- Pi header pins 2 and 4
                                   -- SSR high side (the 5V in the SSR circuit)
DIN GND -- J15 GND -------- GND rail -- Pi ground pins
```

The Pi is powered through the header from this terminal. With the Pi up, its regulator makes 3.3 V on header pins 1 and 17, and that is the 3.3 V rail for the STEMMA jacks, the probe modules, the encoder, and the optocoupler LEDs.

The 5 V loads are the Pi and the two SSR inputs. Size the supply, F2, J15, and the 5 V copper for 5 A, which covers a Pi 5. The SSR inputs add only a few tens of milliamps. Use a power pour from J15 to the header, not a signal trace.

Leave the Pi's USB-C power socket unused. A second 5 V source on this same rail fights the DIN supply. Reversing `+5V` and `GND` on J15 destroys the Pi.

### Still probes

Two 1×8 female sockets, one per MAX31865 module. Both share CLK, SDO, SDI, 3.3 V, and ground. Chip selects stay separate. Power the module's VIN from **3.3 V** so its SPI pins stay at 3.3 V. The `3Vo` pin is a regulator output on the module. Leave it open. `RDY` is unused; the driver waits out the conversion instead of reading it.

These modules are Adafruit MAX31865 clones. The silkscreen order to draw, which must be checked against the module in hand before the footprint is locked, is:

| Socket pin | Silkscreen | Probe 1 (J4) | Probe 2 (J5) |
| --- | --- | --- | --- |
| 1 | VIN | 3.3 V | 3.3 V |
| 2 | 3Vo | open | open |
| 3 | GND | GND | GND |
| 4 | CLK | BCM 11 | BCM 11 |
| 5 | SDO | BCM 9 | BCM 9 |
| 6 | SDI | BCM 10 | BCM 10 |
| 7 | CS | BCM 17 | BCM 8 |
| 8 | RDY | open | open |

Mark pin 1 on the silkscreen. The RTD's own screws stay on the module. Two-wire, three-wire, and four-wire are jumpers on that module, not on the hat.

### I2C hub (STEMMA)

One bus, BCM 2 and BCM 3, hardware I2C1. The LCD and the HTS221 already use STEMMA plugs on a separate 5-port hub. That hub moves onto the hat as five STEMMA QT jacks (JST SH, 1.0 mm). All five are wired in parallel to the same four nets, which is what the Adafruit hub does, so Brewski still sees one bus and does not change.

Pin order on every jack, looking into the plug: 1 GND, 2 3.3 V, 3 SDA, 4 SCL. The 3.3 V pin is the Pi's 3.3 V rail, made from the DIN-rail 5 V input.

| Jack | Label | Cable |
| --- | --- | --- |
| J6 | `LCD` | 20×4 display |
| J7 | `HTS221` | Cabinet sensor |
| J8 | `I2C 3` | Spare |
| J9 | `I2C 4` | Spare |
| J10 | `I2C 5` | Spare |

HTS221 address is `0x5F`. The LCD is probed at `0x27`, then `0x3F`. The Pi's own I2C pull-ups stay on the bus. The external Adafruit hub is not used.

## Connector summary

Screw terminals at 5.08 mm for every field wire. J2 and J3 run to the SSRs on the DIN rail. J13 runs to the panel dial. J15 is the 5 V input from the DIN-rail supply, and it is sized for the Pi's current. The blocks are the pluggable kind, so the hat can come off the Pi without unwiring the cabinet. Female 2.54 mm sockets for the two probe modules. Five STEMMA QT jacks for the I2C hub.

| Ref | Pins | Label | Goes to |
| --- | --- | --- | --- |
| J1 | 2×20 female | Pi | 40-pin header |
| J2 | 2 | `SSR1 +` `SSR1 -` | Control cable to heater 1 SSR on the DIN rail |
| J3 | 2 | `SSR2 +` `SSR2 -` | Control cable to heater 2 SSR on the DIN rail |
| J4 | 1×8 female | `PROBE 1` | MAX31865, chip select BCM 17 |
| J5 | 1×8 female | `PROBE 2` | MAX31865, chip select BCM 8 |
| J6 | 4 | `LCD` | STEMMA QT, 20×4 display |
| J7 | 4 | `HTS221` | STEMMA QT, cabinet sensor |
| J8 | 4 | `I2C 3` | STEMMA QT, spare |
| J9 | 4 | `I2C 4` | STEMMA QT, spare |
| J10 | 4 | `I2C 5` | STEMMA QT, spare |
| J11 | 5 | `START` `NO` `COM` `NC` `L1` `L2` | Green button |
| J12 | 5 | `STOP` `NO` `COM` `NC` `L1` `L2` | Red button |
| J13 | 4 | `ENC` `+3V3` `GND` `A` `B` | Cable to the panel dial |
| J14 | 2 | `LAMP L` `LAMP N` | 110–220 V lamp supply |
| J15 | 2 | `+5V` `GND` | 5 V input from the DIN-rail supply |
| F1 | — | `100mA` | In series with `LAMP L` |
| F2 | — | `5A` | In series with J15 `+5V` |

## Draft bill of materials

Parts the hat adds. The SSRs, probe modules, HTS221, LCD, encoder, and buttons plug in. The separate Qwiic hub does not. The DIN-rail 5 V supply is the power source and is not on this board.

| Qty | Part | Refs | Role |
| --- | --- | --- | --- |
| 1 | 2×20 female header, 2.54 mm | J1 | Pi |
| 2 | Screw terminal, 2-pos, 5.08 mm | J2, J3 | Control cable to the DIN-rail SSRs |
| 1 | Screw terminal, 2-pos, 5.08 mm, ≥10 A | J15 | 5 V input from the DIN rail |
| 1 | Pluggable terminal, 2-pos, 5.08 mm | J14 | Lamp supply |
| 2 | Pluggable terminal, 5-pos, 5.08 mm | J11, J12 | Buttons |
| 1 | Screw terminal, 4-pos, 5.08 mm | J13 | Dial cable: A, B, 3.3 V, ground |
| 2 | Female socket 1×8, 2.54 mm | J4, J5 | Probe modules |
| 5 | STEMMA QT jack, JST SH 4-pin | J6–J10 | Onboard I2C hub |
| 2 | MOC3063 | OK1, OK2 | Lamp optocouplers |
| 2 | 2N3904 | Q1, Q2 | SSR drivers |
| 2 | 220 Ω | R1, R2 | Optocoupler LEDs |
| 4 | 10 kΩ | R3, R4, R7, R8 | Lamp pull-ups, SSR pull-downs |
| 2 | 10 kΩ | R11, R12 | Button pull-ups, 3.3 V to GPIO |
| 2 | 1 kΩ | R5, R6 | SSR base resistors |
| 2 | 39 Ω | R9, R10 | Snubber, optional |
| 2 | 0.01 µF, mains rated | C3, C4 | Snubber, optional |
| 2 | 100 nF | C5, C6 | 3.3 V and 5 V bypass |
| 1 | Fuse, 100 mA, and a holder | F1 | Lamp feed |
| 1 | Fuse, 5 A, and a holder | F2 | 5 V input |
| — | SunFounder 2-channel relay module | — | Removed. No footprint |

## Mechanical

- Take the 40-pin header position and the four M2.5 mounting holes from the Raspberry Pi HAT mechanical drawing. Hole spacing on that drawing is 58 mm by 49 mm. Those holes are what make it bolt to the Pi.
- The outline may extend past 65 × 56.5 mm. Grow the board on the side opposite the header so the lamp section has a clear slot under each optocoupler.
- Components on top. The female header faces the Pi.
- No ID EEPROM, no pogo pins, no cutout required beyond the two optocoupler slots.
- Silkscreen the name `BREWSKI`, a pin-1 mark on every socket, and the labels in the connector table.

## What the design software should produce

From this file, in order:

1. A schematic with the reference designators, values, and nets above. One sheet is enough. Draw a dashed line around the lamp side of OK1, OK2, F1, J14, and the `L1`/`L2` pins. Draw the 5 V net from J15 through F2 to the Pi's 5 V pins and the SSR high sides, and the 3.3 V net from the Pi's 3.3 V pins out to the STEMMA jacks, the probes, and J13.
2. A layout on the HAT hole pattern, with the lamp copper kept to the slotted edge.
3. A 3D check that a Pi 4 or Pi 5, the two probe sockets, and the terminal blocks clear the Pi's own connectors.

Leave these as checks before the footprint is locked, because a wrong one means a respin:

- The HiLetgo MAX31865 pin-1 order, against the silkscreen on the module.
- The lamp and contact marks on the LA155 buttons in hand. The hat connector names the signals; the button body has its own numbering.
- The `+` and `−` marks on each SSR-40DA.
- That the encoder's VCC pin really is the pull-up supply, and that J13's `+3V3` is the pin that lands on it.
- The DIN-rail supply voltage at the terminals, under load. The Pi wants that rail at 5 V.

## Later, on top of this

This revision is the plug-in breakout. Later boards can absorb parts that are modules today: the MAX31865 chips and their reference resistors, the HTS221, and the LCD backpack. The BCM map, the active levels, and the rule that heater AC stays on the SSR do not change when that happens. A low-voltage button lamp would replace the phototriac output only, as described above. A HAT EEPROM can be added on GPIO 0 and GPIO 1 without moving any other net.
