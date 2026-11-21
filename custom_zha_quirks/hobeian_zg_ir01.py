"""HOBEIAN ZG-IR01 Zigbee IR blaster (with temperature/humidity sensor).

Local ZHA quirk. Reuses the Zosung IR protocol from zhaquirks.tuya.ts1201 and
adds what the ZG-IR01 does differently (taken from Zigbee2MQTT,
zigbee-herdsman-converters src/devices/hobeian.ts and src/lib/zosung.ts):

* The device asks for every chunk of an IR message with sequence 0, so sends
  always use seq 0.
* The device is a sleepy end device and sometimes misses the first frame 0x00
  of a send, so it is repeated until the device asks for the first chunk.
* A late frame 0x04 of an earlier transfer (also seq 0) is ignored until the
  device has asked for the first chunk of the current one.
* Chunks sent to the device are at most 0x32 bytes (its maximum incoming
  transfer size is 66 bytes), and our frames ask for no default response.
* Firmware newer than 01062026 (date code) only plays Broadlink-encoded codes.
  The device learns codes in a Broadlink variant ("JgC..."); those and other
  Broadlink codes (e.g. from the SmartIR library) are sent unchanged, as
  Zigbee2MQTT does. Codes in Tuya's compressed format are converted.

Learning:  zha.issue_zigbee_cluster_command  cluster 0xE004 (57348), command 1
           (IRLearn) with params {"on_off": true}. When the code is in, the
           quirk fires a zha_event with command "ir_code_learned" and
           args {"code": "..."}; it can also be read as attribute
           last_learned_ir_code (0x0000) of cluster 0xE004.
Sending:   cluster 0xE004, command 2 (IRSend) with params {"code": "..."},
           optional "freq" (Hz, default 38000) and "repeats" (default 0).

Credits and thanks:
  The ZG-IR01 specifics in this quirk (sequence 0, 50-byte chunks, the frame
  0x00 retry, ignoring stale 0x04 frames and the Broadlink code handling) are
  taken from the Zigbee2MQTT project, zigbee-herdsman-converters
  (https://github.com/Koenkk/zigbee-herdsman-converters, src/devices/hobeian.ts
  and src/lib/zosung.ts, MIT license). Many thanks to Koenkk and all
  Zigbee2MQTT contributors for reverse-engineering this device and the Zosung
  IR protocol - without their groundwork this quirk would not exist.
  The base protocol implementation comes from zha-device-handlers
  (zhaquirks/tuya/ts1201.py, inspired by @mak-42's zosung.js); thanks to its
  contributors as well.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from typing import Any, Final

from zigpy.profiles import zha
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import (
    Basic,
    Groups,
    Identify,
    OnOff,
    PowerConfiguration,
    Scenes,
)
from zigpy.zcl.clusters.measurement import RelativeHumidity, TemperatureMeasurement

from zhaquirks.const import (
    DEVICE_TYPE,
    ENDPOINTS,
    INPUT_CLUSTERS,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
)
from zhaquirks.tuya.ts1201 import (
    DEFAULT_IR_FREQUENCY,
    PendingIRSend,
    ZosungIRBlaster,
    ZosungIRControl,
    ZosungIRTransmit,
    ir_msg_for_code,
    ir_msg_part_crc,
)

_LOGGER = logging.getLogger(__name__)

TUYA_CLUSTER_ID: Final = 0xEF00
BROADLINK_IR: Final = 0x26
# Seconds between repeats of frame 0x00 until the device asks for the first chunk.
SEND_START_RETRY_INTERVAL: Final = 3
# Zigbee2MQTT sends chunks of 0x32 bytes; with the frame overhead that still
# fits the device's 66-byte maximum incoming transfer size.
IR_SEND_PART_MAX_LENGTH: Final = 0x32


# ---------------------------------------------------------------------------
# IR code formats
# ---------------------------------------------------------------------------


def decode_tuya_timings(compressed: bytes) -> list[int]:
    """Decode a Tuya (FastLZ-style) compressed IR code into durations in µs."""
    out = bytearray()
    pos = 0
    while pos < len(compressed):
        header = compressed[pos]
        pos += 1
        block_type = header >> 5
        if block_type == 0:
            length = (header & 0x1F) + 1
            if pos + length > len(compressed):
                raise ValueError("Truncated literal block in IR code")
            out += compressed[pos : pos + length]
            pos += length
            continue
        length = block_type + 2
        if length == 9:
            while pos < len(compressed) and compressed[pos] == 0xFF:
                length += 255
                pos += 1
            if pos >= len(compressed):
                raise ValueError("Truncated length in IR code")
            length += compressed[pos]
            pos += 1
        if pos >= len(compressed):
            raise ValueError("Truncated distance in IR code")
        offset = (((header & 0x1F) << 8) | compressed[pos]) + 1
        pos += 1
        if offset > len(out):
            raise ValueError("Invalid back reference in IR code")
        for _ in range(length):
            out.append(out[-offset])
    if len(out) < 2:
        raise ValueError("IR code holds no timings")
    return [out[i] | (out[i + 1] << 8) for i in range(0, len(out) - 1, 2)]


def timings_to_broadlink(timings: list[int], repeats: int = 0) -> bytes:
    """Encode durations in µs as a Broadlink IR packet."""
    if not timings:
        raise ValueError("IR timings must not be empty")
    encoded = bytearray()
    for timing in timings:
        duration = abs(int(timing))
        if duration <= 0:
            raise ValueError(f"Invalid IR timing: {timing}")
        ticks = max(1, round(duration * 269 / 8192))
        if ticks > 0xFFFF:
            raise ValueError(f"IR timing too long: {timing}")
        if ticks < 0x100:
            encoded.append(ticks)
        else:
            encoded += bytes((0x00, ticks >> 8, ticks & 0xFF))
    packet = bytearray((BROADLINK_IR, max(0, min(255, repeats))))
    packet += len(encoded).to_bytes(2, "little")
    packet += encoded
    packet += b"\x0d\x05"
    while (len(packet) + 4) % 16:
        packet.append(0)
    return bytes(packet)


def is_broadlink(raw: bytes) -> bool:
    """Return True if raw looks like a Broadlink IR packet.

    The ZG-IR01 learns codes in a Broadlink variant whose length field holds the
    total packet length and that has no 0x0d 0x05 trailer, so only the IR marker
    is checked. A Tuya stream can never start with 0x26 (its first block is a
    literal block, header < 0x20).
    """
    return len(raw) >= 4 and raw[0] == BROADLINK_IR


def to_broadlink_code(code: str, repeats: int = 0) -> str:
    """Return code as a Broadlink base64 string, converting Tuya codes."""
    try:
        raw = base64.b64decode(code.strip(), validate=True)
    except ValueError as exc:
        raise ValueError("IR code is not valid base64") from exc
    if is_broadlink(raw):
        if repeats:
            raw = bytes((raw[0], max(0, min(255, repeats)))) + raw[2:]
        return base64.b64encode(raw).decode()
    # A Tuya stream always starts with a literal block (header < 0x20).
    if not raw or raw[0] >= 0x20:
        raise ValueError("IR code is neither Broadlink nor Tuya format")
    return base64.b64encode(
        timings_to_broadlink(decode_tuya_timings(raw), repeats)
    ).decode()


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------


class ZGIR01IRControl(ZosungIRControl):
    """Zosung IR control cluster that sends Broadlink codes."""

    async def command(
        self,
        command_id: foundation.GeneralCommand | int | t.uint8_t,
        *args,
        manufacturer: int | t.uint16_t | None = None,
        expect_reply: bool = True,
        tsn: int | t.uint8_t | None = None,
        **kwargs: Any,
    ):
        """Convert the code of an IRSend to Broadlink, then send it."""
        if command_id != self.ServerCommandDefs.IRSend.id:
            return await super().command(
                command_id,
                *args,
                manufacturer=manufacturer,
                expect_reply=expect_reply,
                tsn=tsn,
                **kwargs,
            )
        repeats = kwargs.get("repeats") or 0
        code = to_broadlink_code(str(kwargs["code"]), repeats)
        # The repeat count now lives in the Broadlink packet, so the message
        # itself asks for a single transmission, as Zigbee2MQTT does.
        ir_msg = ir_msg_for_code(
            code, freq=kwargs.get("freq") or DEFAULT_IR_FREQUENCY, repeats=0
        )
        _LOGGER.debug("Sending IR code %s to %s", ir_msg, self.endpoint.device.ieee)
        await self.endpoint.zosung_irtransmit.send_ir_msg(ir_msg, tsn=tsn)
        return foundation.GENERAL_COMMANDS[
            foundation.GeneralCommand.Default_Response
        ].schema(command_id=command_id, status=foundation.Status.SUCCESS)


class ZGIR01IRTransmit(ZosungIRTransmit):
    """Zosung IR transmit cluster with the ZG-IR01's quirks.

    Learning (device -> us) is handled by the base class unchanged. The send
    side (us -> device) follows zigbee-herdsman-converters lib/zosung.ts:
    seq 0, frame 0x00 repeated until the first chunk request, chunks of at most
    0x32 bytes (the device only takes 66-byte frames, so the 0x38 the base class
    would send does not fit), frame 0x04 before the first chunk request ignored,
    and every frame we send without a default response.
    """

    def __init__(self, *args, **kwargs):
        """Init cluster."""
        super().__init__(*args, **kwargs)
        self.send_started = False

    async def _send(self, command_id: int, **kwargs) -> None:
        """Send a frame the way Zigbee2MQTT does (no default response)."""
        await super(ZosungIRTransmit, self).command(
            command_id,
            expect_reply=False,
            disable_default_response=True,
            **kwargs,
        )

    async def _transfer_ir_msg(
        self, ir_msg: bytes, *, tsn: int | t.uint8_t | None = None
    ) -> None:
        """Run one transfer, always with seq 0, repeating frame 0x00 as needed."""
        self.seq = 0
        self.send_started = False
        loop = asyncio.get_running_loop()
        pending = PendingIRSend(0, ir_msg, loop.create_future(), loop.create_future())
        self.pending_send = pending

        async def send_start() -> None:
            await self._send(
                self.ServerCommandDefs.receive_ir_frame_00.id,
                seq=0,
                length=len(ir_msg),
                unk1=0x00000000,
                clusterid=ZosungIRControl.cluster_id,
                unk2=0x01,
                cmd=ZosungIRControl.ServerCommandDefs.IRSend.id,
                unk3=0x0000,
                tsn=tsn,
            )

        async def repeat_start() -> None:
            while not self.send_started:
                await asyncio.sleep(SEND_START_RETRY_INTERVAL)
                if self.send_started or pending.confirmed.done():
                    return
                _LOGGER.debug("Repeating IR frame 0x00 to %s", self.endpoint.device.ieee)
                try:
                    await send_start()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Repeating IR frame 0x00 failed: %r", exc)

        retry = loop.create_task(repeat_start())
        try:
            await send_start()
            async with asyncio.timeout(self.send_timeout):
                await pending.confirmed
        except TimeoutError:
            _LOGGER.warning(
                "IR code was not confirmed by %s", self.endpoint.device.ieee
            )
            raise
        else:
            await pending.closed
        finally:
            retry.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retry
            self.pending_send = None

    async def _send_part(self, seq: int, position: int, part: bytes) -> None:
        """Send one chunk of the IR message (frame 0x03)."""
        await self._send(
            self.ServerCommandDefs.receive_ir_frame_03.id,
            zero=0,
            seq=seq,
            position=position,
            msgpart=part,
            msgpartcrc=ir_msg_part_crc(part),
        )

    async def _finish_send(self, seq: int, pending: PendingIRSend) -> None:
        """Answer frame 0x04 with 0x05, then let the next send start."""
        try:
            await self._send(
                self.ServerCommandDefs.receive_ir_frame_05.id, seq=seq, zero=0
            )
        finally:
            if not pending.closed.done():
                pending.closed.set_result(None)

    def handle_cluster_request(
        self,
        hdr: foundation.ZCLHeader,
        args: list[Any],
        *,
        dst_addressing: t.AddrMode | None = None,
    ):
        """Handle the send side ourselves, leave learning to the base class."""
        command_id = hdr.command_id
        pending = self.pending_send

        if command_id == self.ServerCommandDefs.receive_ir_frame_02.id:
            if not hdr.frame_control.disable_default_response:
                self.send_default_rsp(hdr, status=foundation.Status.SUCCESS)
            if pending is None:
                _LOGGER.warning(
                    "Cannot send IR code to %s: no pending message",
                    self.endpoint.device.ieee,
                )
                return
            self.send_started = True
            position = args.position
            size = min(args.maxlen or IR_SEND_PART_MAX_LENGTH, IR_SEND_PART_MAX_LENGTH)
            part = pending.msg[position : position + size]
            _LOGGER.debug(
                "IR chunk request from %s: position %s, sending %s bytes",
                self.endpoint.device.ieee,
                position,
                len(part),
            )
            self.create_catching_task(self._send_part(args.seq, position, part))
            return

        if (
            command_id == self.ServerCommandDefs.receive_ir_frame_04.id
            and pending is not None
        ):
            if not hdr.frame_control.disable_default_response:
                self.send_default_rsp(hdr, status=foundation.Status.SUCCESS)
            if not self.send_started:
                _LOGGER.debug(
                    "Ignoring IR frame 0x04 from %s before the first chunk request",
                    self.endpoint.device.ieee,
                )
                return
            _LOGGER.debug("IR code sent by %s", self.endpoint.device.ieee)
            if not pending.confirmed.done():
                pending.confirmed.set_result(None)
            self.create_catching_task(self._finish_send(args.seq, pending))
            return

        before = self.endpoint.device.last_learned_ir_code
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)
        learned = self.endpoint.device.last_learned_ir_code
        if (
            command_id == self.ServerCommandDefs.receive_ir_frame_05.id
            and learned
            and learned != before
        ):
            self.endpoint.zosung_ircontrol.listener_event(
                "zha_send_event", "ir_code_learned", {"code": str(learned)}
            )


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------


class HobeianZGIR01(ZosungIRBlaster):
    """HOBEIAN ZG-IR01 IR blaster."""

    signature = {
        MODELS_INFO: [("HOBEIAN", "ZG-IR01")],
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: zha.DeviceType.ON_OFF_LIGHT,
                INPUT_CLUSTERS: [
                    Basic.cluster_id,
                    PowerConfiguration.cluster_id,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    OnOff.cluster_id,
                    TemperatureMeasurement.cluster_id,
                    RelativeHumidity.cluster_id,
                    ZosungIRControl.cluster_id,
                    ZosungIRTransmit.cluster_id,
                    TUYA_CLUSTER_ID,
                ],
                OUTPUT_CLUSTERS: [Identify.cluster_id],
            },
        },
    }

    # OnOff is left out on purpose: in ZHA it only produced a light entity
    # that does nothing useful on this device.
    replacement = {
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: zha.DeviceType.ON_OFF_LIGHT,
                INPUT_CLUSTERS: [
                    Basic.cluster_id,
                    PowerConfiguration.cluster_id,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    TemperatureMeasurement.cluster_id,
                    RelativeHumidity.cluster_id,
                    ZGIR01IRControl,
                    ZGIR01IRTransmit,
                    TUYA_CLUSTER_ID,
                ],
                OUTPUT_CLUSTERS: [Identify.cluster_id],
            },
        },
    }
