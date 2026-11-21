import asyncio
import base64
import importlib.util
import pathlib
import sys
from unittest import mock

import pytest
import zigpy.device
import zigpy.endpoint
import zigpy.quirks
import zigpy.types as t
from zigpy.zcl import foundation
import zhaquirks

zhaquirks.setup()

QUIRK = str(pathlib.Path(__file__).resolve().parents[1] / "custom_zha_quirks" / "hobeian_zg_ir01.py")
spec = importlib.util.spec_from_file_location("hobeian_zg_ir01", QUIRK)
q = importlib.util.module_from_spec(spec)
sys.modules["hobeian_zg_ir01"] = q
spec.loader.exec_module(q)


def make_device():
    app = mock.MagicMock()
    app.request = mock.AsyncMock(return_value=(foundation.Status.SUCCESS, "ok"))
    app.send_packet = mock.AsyncMock()
    ieee = t.EUI64.convert("a4:c1:38:00:00:00:00:01")
    dev = zigpy.device.Device(app, ieee, 0x79F5)
    dev.node_desc = zigpy.zdo.types.NodeDescriptor(
        logical_type=2, complex_descriptor_available=0, user_descriptor_available=0,
        reserved=0, aps_flags=0, frequency_band=8, mac_capability_flags=128,
        manufacturer_code=4742, maximum_buffer_size=66, maximum_incoming_transfer_size=66,
        server_mask=10752, maximum_outgoing_transfer_size=66, descriptor_capability_field=0)
    ep = dev.add_endpoint(1)
    ep.profile_id = 260
    ep.device_type = 0x0100
    for c in [0x0000, 0x0003, 0x0004, 0x0005, 0xEF00, 0xE004, 0xED00, 0x0006, 0x0402, 0x0405, 0x0001]:
        ep.add_input_cluster(c)
    ep.add_output_cluster(0x0003)
    dev.manufacturer = "HOBEIAN"
    dev.model = "ZG-IR01"
    return dev


def test_quirk_matches():
    dev = zigpy.quirks.get_device(make_device())
    assert isinstance(dev, q.HobeianZGIR01)
    ep = dev.endpoints[1]
    assert isinstance(ep.zosung_ircontrol, q.ZGIR01IRControl)
    assert isinstance(ep.zosung_irtransmit, q.ZGIR01IRTransmit)
    assert 0x0006 not in ep.in_clusters
    assert 0x0402 in ep.in_clusters and 0x0405 in ep.in_clusters


def tuya_encode(timings):
    raw = bytearray()
    for v in timings:
        raw += int(v).to_bytes(2, "little")
    out = bytearray()
    for i in range(0, len(raw), 32):
        chunk = raw[i:i + 32]
        out.append(len(chunk) - 1)
        out += chunk
    return bytes(out)


def test_tuya_to_broadlink():
    timings = [9000, 4500] + [560, 560, 560, 1690] * 16 + [560]
    tuya = base64.b64encode(tuya_encode(timings)).decode()
    assert q.decode_tuya_timings(base64.b64decode(tuya)) == timings
    bl = base64.b64decode(q.to_broadlink_code(tuya))
    assert bl[0] == 0x26 and q.is_broadlink(bl)
    assert (len(bl) + 4) % 16 == 0
    # 9000 µs -> 296 ticks -> 0x00 0x01 0x28
    assert bl[4:7] == bytes((0, 1, 0x28))


def test_tuya_backref_decode():
    # literal "AB" then reference copying 4 bytes from distance 2 -> "ABABAB"
    data = bytes((1, 0x41, 0x42, (2 << 5) | 0, 1))
    assert q.decode_tuya_timings(data) == [0x4241, 0x4241, 0x4241]


def test_broadlink_passthrough_and_repeats():
    pkt = q.timings_to_broadlink([9000, 4500, 560])
    code = base64.b64encode(pkt).decode()
    assert q.to_broadlink_code(code) == code
    assert base64.b64decode(q.to_broadlink_code(code, repeats=2))[1] == 2


def test_rejects_garbage():
    with pytest.raises(ValueError):
        q.to_broadlink_code("not base64!!")
    with pytest.raises(ValueError):
        q.to_broadlink_code(base64.b64encode(b"\xff\x00\x01").decode())


def hdr(cmd):
    h = foundation.ZCLHeader.cluster(tsn=1, command_id=cmd)
    return h


@pytest.mark.asyncio
async def test_send_flow_seq0_retry_and_stale04(monkeypatch):
    monkeypatch.setattr(q, "SEND_START_RETRY_INTERVAL", 0.05)
    dev = zigpy.quirks.get_device(make_device())
    ep = dev.endpoints[1]
    tx = ep.zosung_irtransmit
    ctrl = ep.zosung_ircontrol
    sent = []

    async def fake_request(*args, **kwargs):
        data = args[5] if len(args) > 5 else kwargs["data"]
        cluster = args[1] if len(args) > 1 else kwargs["cluster"]
        sent.append((None, cluster, None, None, bytes(data)))
        return (foundation.Status.SUCCESS, "ok")

    monkeypatch.setattr(dev, "request", fake_request)
    tuya = base64.b64encode(tuya_encode([9000, 4500, 560, 560, 560])).decode()
    task = asyncio.create_task(ctrl.command(2, code=tuya))
    await asyncio.sleep(0.18)
    frame00 = [s for s in sent if s[1] == 0xED00 and s[4][2] == 0x00]
    assert len(frame00) >= 2, "frame 0x00 should be repeated"
    # stale 0x04 before first chunk request must be ignored
    s04 = tx.ServerCommandDefs.receive_ir_frame_04.schema(zero0=0, seq=0, zero1=0)
    tx.handle_cluster_request(hdr(0x04), s04)
    assert not tx.pending_send.confirmed.done()
    msg = tx.pending_send.msg
    assert b'"key_code":"J' in msg  # Broadlink base64 starts with 0x26 -> "J"
    pos = 0
    while pos < len(msg):
        s02 = tx.ServerCommandDefs.receive_ir_frame_02.schema(seq=0, position=pos, maxlen=0x38)
        tx.handle_cluster_request(hdr(0x02), s02)
        pos += 0x38
    await asyncio.sleep(0.01)
    n00 = len([s for s in sent if s[1] == 0xED00 and s[4][2] == 0x00])
    await asyncio.sleep(0.15)
    assert len([s for s in sent if s[1] == 0xED00 and s[4][2] == 0x00]) == n00, "retry stops"
    tx.handle_cluster_request(hdr(0x04), s04)
    result = await asyncio.wait_for(task, 2)
    assert result.status == foundation.Status.SUCCESS
    assert tx.pending_send is None


@pytest.mark.asyncio
async def test_learn_fires_event(monkeypatch):
    dev = zigpy.quirks.get_device(make_device())
    ep = dev.endpoints[1]
    tx = ep.zosung_irtransmit
    ctrl = ep.zosung_ircontrol

    async def fake_request(*args, **kwargs):
        return (foundation.Status.SUCCESS, "ok")

    monkeypatch.setattr(dev, "request", fake_request)
    events = []
    listener = mock.MagicMock()
    listener.zha_send_event = lambda cmd, arg: events.append((cmd, arg))
    ctrl.add_listener(listener)
    payload = tuya_encode([9000, 4500, 560])
    s00 = tx.ServerCommandDefs.receive_ir_frame_00.schema(
        seq=7, length=len(payload), unk1=0, clusterid=0xE004, unk2=1, cmd=1, unk3=0)
    tx.handle_cluster_request(hdr(0x00), s00)
    s03 = tx.ServerCommandDefs.receive_ir_frame_03.schema(
        zero=0, seq=7, position=0, msgpart=t.LVBytes(payload), msgpartcrc=sum(payload) % 256)
    tx.handle_cluster_request(hdr(0x03), s03)
    s05 = tx.ServerCommandDefs.receive_ir_frame_05.schema(seq=7, zero=0)
    tx.handle_cluster_request(hdr(0x05), s05)
    await asyncio.sleep(0.01)
    code = base64.b64encode(payload).decode()
    assert events == [("ir_code_learned", {"code": code})]
    ok, _ = await ctrl.read_attributes(["last_learned_ir_code"])
    assert ok[0] == code


@pytest.mark.asyncio
async def test_zha_style_calls(monkeypatch):
    from zha.zigbee.device import convert_to_zcl_values  # noqa
    dev = zigpy.quirks.get_device(make_device())
    ctrl = dev.endpoints[1].zosung_ircontrol
    seen = []

    async def fake_request(*args, **kwargs):
        data = args[5] if len(args) > 5 else kwargs["data"]
        seen.append(bytes(data))
        return (foundation.Status.SUCCESS, "ok")

    monkeypatch.setattr(dev, "request", fake_request)
    cmd = ctrl.server_commands[1]
    await getattr(ctrl, cmd.name)(**convert_to_zcl_values({"on_off": True}, cmd.schema))
    assert b'{"study":0}' in seen[-1]
    cmd2 = ctrl.server_commands[2]
    params = convert_to_zcl_values({"code": "not base64!!"}, cmd2.schema)
    with pytest.raises(ValueError):
        await getattr(ctrl, cmd2.name)(**params)


def test_device_learned_broadlink_passthrough():
    # Code learned by the real ZG-IR01 (length field = total length, no 0d05 trailer)
    code = "JgCNAIyOEDYQNhA2EBIQExASEBIQEhA2EDUQNhASEBMQEhASEBMQEhA2EBIQEhASEBIQEhASEDYQExA2EDYQNRA2EDYQNhAABb2MjRA2EDYQNhATEBIQEhASEBIQNRA2ETUQEhASEBIQExASEBMQNhASEBIQFA4TEBIQEhA2EBIQNhA2EDYQNhA2EDYQ"
    assert q.to_broadlink_code(code) == code


@pytest.mark.asyncio
async def test_send_chunks_like_z2m(monkeypatch):
    monkeypatch.setattr(q, "SEND_START_RETRY_INTERVAL", 10)
    dev = zigpy.quirks.get_device(make_device())
    ep = dev.endpoints[1]
    tx, ctrl = ep.zosung_irtransmit, ep.zosung_ircontrol
    frames = []

    async def fake_request(*args, **kwargs):
        data = bytes(args[5] if len(args) > 5 else kwargs["data"])
        cluster = args[1] if len(args) > 1 else kwargs["cluster"]
        frames.append((cluster, data))
        return (foundation.Status.SUCCESS, "ok")

    monkeypatch.setattr(dev, "request", fake_request)
    code = "JgCNAIyOEDYQNhA2EBIQExASEBIQEhA2EDUQNhASEBMQEhASEBMQEhA2EBIQEhASEBIQEhASEDYQExA2EDYQNRA2EDYQNhAABb2MjRA2EDYQNhATEBIQEhASEBIQNRA2ETUQEhASEBIQExASEBMQNhASEBIQFA4TEBIQEhA2EBIQNhA2EDYQNhA2EDYQ"
    task = asyncio.create_task(ctrl.command(2, code=code))
    await asyncio.sleep(0.05)
    msg = tx.pending_send.msg
    assert code.encode() in msg
    # every frame we send asks for no default response (fc bit 0x10)
    f00 = [d for c, d in frames if c == 0xED00 and d[2] == 0x00]
    assert f00 and all(d[0] & 0x10 for d in f00)
    got = bytearray()
    while len(got) < len(msg):
        # device asks with its own maxlen 0x38; we must answer with <= 0x32
        tx.handle_cluster_request(hdr(0x02), tx.ServerCommandDefs.receive_ir_frame_02.schema(seq=0, position=len(got), maxlen=0x38))
        await asyncio.sleep(0.01)
        d = [d for c, d in frames if c == 0xED00 and d[2] == 0x03][-1]
        assert d[0] & 0x10
        body = d[3:]
        pos = int.from_bytes(body[3:7], "little")
        ln = body[7]
        part = body[8:8 + ln]
        assert pos == len(got) and 0 < ln <= 0x32
        assert body[8 + ln] == sum(part) % 256
        assert len(d) <= 66 - 4  # fits device buffer with room to spare
        got += part
    assert bytes(got) == msg
    tx.handle_cluster_request(hdr(0x04), tx.ServerCommandDefs.receive_ir_frame_04.schema(zero0=0, seq=0, zero1=0))
    res = await asyncio.wait_for(task, 2)
    assert res.status == foundation.Status.SUCCESS
    assert any(c == 0xED00 and d[2] == 0x05 and d[0] & 0x10 for c, d in frames)
