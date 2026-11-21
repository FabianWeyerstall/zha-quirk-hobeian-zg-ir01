# ZHA quirk for the HOBEIAN ZG-IR01 Zigbee IR blaster

The **HOBEIAN ZG-IR01** (Tuya/Zosung based IR blaster with temperature and humidity sensor) pairs
with ZHA, but out of the box ZHA applies no quirk: you get temperature, humidity and battery, and the
IR clusters `0xE004` / `0xED00` are unusable. This repository adds a local ZHA quirk that makes
**learning and sending IR codes** work, plus an optional Home Assistant package and dashboard card to
use it without writing YAML.

Tested with Home Assistant 2026.11.2 (zha 2.4.3, zha-quirks 2.4.1), firmware date code `18092026`.

*Deutsch: Kurzanleitung weiter unten.*

## What the quirk does

It reuses the Zosung IR protocol from `zhaquirks.tuya.ts1201` (the TS1201 / ZS06 quirk) and adds what
the ZG-IR01 does differently, following Zigbee2MQTT
(`zigbee-herdsman-converters`, `src/devices/hobeian.ts` and `src/lib/zosung.ts`):

| ZG-IR01 behaviour | Handling in the quirk |
|---|---|
| Requests every chunk of a send with sequence `0` | sends always use seq `0` |
| Sleepy end device, sometimes misses the first frame `0x00` | `0x00` is repeated every 3 s until the first chunk request |
| Late `0x04` from a previous transfer (also seq `0`) | ignored until the device asked for the first chunk |
| Max. incoming transfer size 66 bytes | chunks of at most `0x32` bytes (the TS1201 quirk sends `0x38`, which does not fit) |
| Firmware > `01062026` only plays Broadlink-encoded codes | learned codes (Broadlink variant, `JgC…`) and Broadlink codes (e.g. SmartIR) are sent unchanged; Tuya-compressed codes are converted to Broadlink |
| — | fires a `zha_event` `ir_code_learned` with the code in `args.code` when a code was learned |

The `OnOff` cluster is dropped from the replacement, so the useless `light` entity ZHA created disappears.

## Installation

1. Copy `custom_zha_quirks/hobeian_zg_ir01.py` to `/config/custom_zha_quirks/`.
2. Add to `configuration.yaml`:
   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks/
   ```
3. Restart Home Assistant. The device's Zigbee info should show quirk `hobeian_zg_ir01.HobeianZGIR01`.

## Usage (plain ZHA actions)

Learn (point the original remote at the blaster within ~60 s):

```yaml
action: zha.issue_zigbee_cluster_command
data:
  ieee: <your device ieee>
  endpoint_id: 1
  cluster_id: 57348        # 0xE004
  cluster_type: in
  command: 1               # IRLearn
  command_type: server
  params: {on_off: true}
```

The learned code arrives as a `zha_event` with `command: ir_code_learned` and `args.code`
(also readable as attribute `last_learned_ir_code` of cluster `0xE004` until the next restart).

Send:

```yaml
action: zha.issue_zigbee_cluster_command
data:
  ieee: <your device ieee>
  endpoint_id: 1
  cluster_id: 57348
  cluster_type: in
  command: 2               # IRSend
  command_type: server
  params:
    code: "JgC..."         # learned code or any Broadlink code
    # repeats: 1           # optional
    # freq: 38000          # optional, Hz
```

The blaster sleeps on battery: the first send after a pause can take a few seconds (up to ~10 s),
following sends take about one second.

## Optional: remote UI

- `examples/package_ir_remote.yaml` – Home Assistant package with helpers and scripts:
  learn a button under a name (`Gerät | Taste`), store codes in a Local To-do list (`todo.ir_codes`,
  because codes are longer than the 255 characters an `input_text` can hold), play them back via one
  or all blasters (blasters are discovered automatically), fixed blaster per device, battery warning.
- `card/ir-remote-card.js` – custom Lovelace card: learned buttons as a button grid per device,
  blaster selection, delete mode.
- `examples/dashboard.yaml` – dashboard with a remote page and a learning page.

## Tests

```bash
pip install -r requirements-test.txt
pytest -q tests
```

The tests run the quirk against zha-quirks 2.4.1 / zha 2.4.3: signature match, Tuya ↔ Broadlink
conversion, a real learned code, and the full send/learn frame exchange.

## Credits and thanks

**A big thank you to [Zigbee2MQTT](https://www.zigbee2mqtt.io/)** – Koenkk and all contributors of
[zigbee-herdsman-converters](https://github.com/Koenkk/zigbee-herdsman-converters). Everything that
makes this quirk work on the ZG-IR01 (sequence 0, 50-byte chunks, the start-frame retry, ignoring
stale end frames, the Broadlink code handling) was taken from their `src/devices/hobeian.ts` and
`src/lib/zosung.ts`. We built on their reverse-engineering of this device and the Zosung IR protocol;
without that groundwork this quirk would not exist. Zigbee2MQTT's converters are MIT licensed.

Thanks as well to the [zha-device-handlers](https://github.com/zigpy/zha-device-handlers) contributors
for the TS1201 Zosung implementation (`zhaquirks/tuya/ts1201.py`, inspired by @mak-42's `zosung.js`)
that this quirk extends.

License: Apache-2.0, like zha-device-handlers.

*Danke an Zigbee2MQTT:* Die gerätespezifischen Details stammen aus deren Arbeit. Ohne ihre Vorarbeit gäbe es diesen Quirk nicht.

---

## Kurzanleitung (Deutsch)

1. `custom_zha_quirks/hobeian_zg_ir01.py` nach `/config/custom_zha_quirks/` kopieren.
2. In der `configuration.yaml` unter `zha:` die Zeile `custom_quirks_path: /config/custom_zha_quirks/` ergänzen.
3. Home Assistant neu starten.
4. Lernen mit Cluster `0xE004`, Befehl 1. Senden mit Befehl 2 und dem Code, siehe oben.
   Für die Bedienung per Dashboard das Paket aus `examples/` und die Karte aus `card/` verwenden.
