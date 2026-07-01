# SPDX-FileCopyrightText: 2014-2025 Espressif Systems (Shanghai) CO LTD,
# other contributors as noted.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Unit tests for the ``lost_response_resends`` workaround (re-sending idempotent
# requests on a flaky transport) and the narrow ``SerialReaderStoppedError``
# exception that drives it. These tests use a fake serial port and do not need
# any hardware.

import struct

import pytest

import esptool.loader as loader_mod
from esptool.loader import ESPLoader, slip_reader
from esptool.util import FatalError, SerialReaderStoppedError, UnsupportedCommandError


def slip_encode(payload):
    return (
        b"\xc0"
        + payload.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc")
        + b"\xc0"
    )


def resp(op_ret, val=0, data=b""):
    """Build a SLIP-encoded response packet."""
    return slip_encode(struct.pack("<BBHI", 0x01, op_ret, len(data), val) + data)


def invalid_cmd_resp(sent_op):
    """An 'invalid command' reply, as if the request was corrupted on the wire
    (op echoed back differs from what we sent, status bytes flag invalid)."""
    other_op = (sent_op + 1) & 0xFF
    return resp(other_op, data=bytes([1, ESPLoader.ROM_INVALID_RECV_MSG]))


class FakePort:
    """Minimal serial-port stand-in. ``chunks`` are returned by successive
    read() calls; ``b""`` models a read timeout (no data)."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.timeout = 0.1
        self.written = []

    def read(self, size=1):
        return self._chunks.pop(0) if self._chunks else b""

    def inWaiting(self):
        return len(self._chunks[0]) if self._chunks else 0

    def write(self, data):
        self.written.append(data)

    def flushInput(self):
        pass


def make_esp(port):
    esp = ESPLoader.__new__(ESPLoader)
    esp._trace_enabled = False
    esp.secure_download_mode = False
    esp._port = port
    esp._slip_reader = slip_reader(port, esp.trace)
    return esp


# --- exception type / slip_reader narrowing ---------------------------------


def test_serial_reader_stopped_is_fatalerror_subclass():
    # Existing `except FatalError` handlers must keep catching it.
    assert issubclass(SerialReaderStoppedError, FatalError)


def test_slip_reader_empty_raises_narrow():
    with pytest.raises(SerialReaderStoppedError):
        next(slip_reader(FakePort([]), lambda *a, **k: None))


def test_slip_reader_partial_packet_is_not_narrow():
    # Start-of-packet byte then a timeout mid-packet -> plain FatalError.
    gen = slip_reader(FakePort([b"\xc0\x01\x02", b""]), lambda *a, **k: None)
    with pytest.raises(FatalError) as exc:
        next(gen)
    assert not isinstance(exc.value, SerialReaderStoppedError)


def test_slip_reader_panic_is_not_narrow():
    gen = slip_reader(FakePort([b"Guru Meditation Error: foo"]), lambda *a, **k: None)
    with pytest.raises(FatalError) as exc:
        next(gen)
    assert not isinstance(exc.value, SerialReaderStoppedError)
    assert "Guru Meditation" in str(exc.value)


def test_read_maps_stopiteration_to_narrow():
    esp = make_esp(FakePort([]))
    esp._slip_reader = iter(())  # exhausted generator -> StopIteration
    with pytest.raises(SerialReaderStoppedError):
        esp.read()


# --- lost-response resend (allow_resend) ------------------------------------


def test_no_resend_when_disabled(monkeypatch):
    monkeypatch.setattr(loader_mod, "LOST_RESPONSE_RESENDS", 5)
    port = FakePort([b""])  # immediate stop
    esp = make_esp(port)
    with pytest.raises(SerialReaderStoppedError):
        esp.command(op=ESPLoader.ESP_CMDS["READ_REG"], allow_resend=False)
    assert len(port.written) == 1  # only the original request, no resend


def test_resend_recovers_lost_response(monkeypatch):
    monkeypatch.setattr(loader_mod, "LOST_RESPONSE_RESENDS", 5)
    op = ESPLoader.ESP_CMDS["READ_REG"]
    port = FakePort([b"", resp(op, val=0x1234)])
    esp = make_esp(port)
    val, _ = esp.command(op=op, allow_resend=True)
    assert val == 0x1234
    assert len(port.written) == 2  # original + 1 resend


def test_resend_on_stale_opcode_reply(monkeypatch):
    """Wrong-op reply in the RX backlog should trigger an immediate re-send."""
    monkeypatch.setattr(loader_mod, "LOST_RESPONSE_RESENDS", 5)
    monkeypatch.setattr(loader_mod.time, "sleep", lambda *a, **k: None)
    op = ESPLoader.ESP_CMDS["READ_REG"]
    stale = resp(ESPLoader.ESP_CMDS["SYNC"], val=0)
    port = FakePort([stale, resp(op, val=0x5678)])
    esp = make_esp(port)
    val, _ = esp.command(op=op, allow_resend=True)
    assert val == 0x5678
    assert len(port.written) == 2  # original + 1 resend after stale SYNC echo


def test_resend_is_bounded(monkeypatch):
    monkeypatch.setattr(loader_mod, "LOST_RESPONSE_RESENDS", 2)
    port = FakePort([b"", b"", b"", b""])  # never recovers
    esp = make_esp(port)
    with pytest.raises(SerialReaderStoppedError):
        esp.command(op=ESPLoader.ESP_CMDS["READ_REG"], allow_resend=True)
    assert len(port.written) == 3  # original + exactly 2 resends


# --- invalid-command reply is never masked by allow_resend -------------------


def test_invalid_command_is_never_resent(monkeypatch):
    # allow_resend recovers lost responses only. A real "invalid command" reply
    # (the request reached the ROM, which rejected the opcode) must surface as
    # UnsupportedCommandError immediately, never re-sent.
    monkeypatch.setattr(loader_mod, "LOST_RESPONSE_RESENDS", 5)
    monkeypatch.setattr(loader_mod.time, "sleep", lambda *a, **k: None)
    op = ESPLoader.ESP_CMDS["SPI_FLASH_MD5"]
    port = FakePort([invalid_cmd_resp(op)])
    esp = make_esp(port)
    with pytest.raises(UnsupportedCommandError):
        esp.command(op=op, allow_resend=True)
    assert len(port.written) == 1  # only the original request, no resend


# --- get_security_info() transport-failure vs ESP32-S2 short-response --------


def test_get_security_info_propagates_transport_failure():
    # A lost response (after resends are exhausted) must surface as a transport
    # error, not be masked by the 12-byte ESP32-S2 fallback.
    esp = make_esp(FakePort([]))
    esp.cache = {"security_info": None}
    calls = []

    def fake_check_command(*args, **kwargs):
        calls.append(kwargs.get("resp_data_len"))
        raise SerialReaderStoppedError("link died")

    esp.check_command = fake_check_command
    with pytest.raises(SerialReaderStoppedError):
        esp.get_security_info()
    assert calls == [20]  # only the 20-byte attempt, no 12-byte fallback


def test_get_security_info_falls_back_on_short_response():
    # A non-transport FatalError (e.g. ESP32-S2's shorter response) still falls
    # back to the 12-byte request.
    esp = make_esp(FakePort([]))
    esp.cache = {"security_info": None}
    esp.flush_input = lambda: None
    calls = []

    def fake_check_command(*args, **kwargs):
        rlen = kwargs.get("resp_data_len")
        calls.append(rlen)
        if rlen == 20:
            raise FatalError("Only got 12 byte status response.")
        return struct.pack("<IBBBBBBBB", 0, 0, 0, 0, 0, 0, 0, 0, 0)  # 12 bytes

    esp.check_command = fake_check_command
    si = esp.get_security_info()
    assert calls == [20, 12]
    assert si["chip_id"] is None  # ESP32-S2 path
