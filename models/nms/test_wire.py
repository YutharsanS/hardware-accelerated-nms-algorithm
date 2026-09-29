"""The reply decoder and sequence counter: the host-side checks nothing else covers.

The frame encoder has no test here on purpose. ``vectors.build_frame`` calls
``wire.encode_frame``, and ``test_vectors`` checks every committed frame against the wire spec
field by field, so a second test would only repeat it.
"""

from __future__ import annotations

import pytest

from models.nms import batches, host, wire
from models.nms import params as p


def test_decode_accepts_ok_and_reject_replies() -> None:
    ok = wire.decode_reply(bytes([p.STATUS_OK, 0x41, 0xDE, 0xAD, 0xBE, 0xEF]), 0x41)
    assert ok == wire.Reply(status=p.STATUS_OK, seq=0x41, keep_mask=0xDEADBEEF)
    assert ok.ok
    rej = wire.decode_reply(bytes([p.STATUS_CRC_FAIL, 0x07, 0, 0, 0, 0]), 0x07)
    assert rej.status == p.STATUS_CRC_FAIL
    assert not rej.ok


@pytest.mark.parametrize(
    ("raw", "seq", "why"),
    [
        (bytes([p.STATUS_OK, 0x10, 0, 0, 0]), 0x10, "reply is 5 bytes"),
        (bytes([p.STATUS_OK, 0x11, 0, 0, 0, 1]), 0x10, "answers seq 0x11"),
        (bytes([0x7F, 0x10, 0, 0, 0, 0]), 0x10, "unknown status"),
        (bytes([p.STATUS_BUSY, 0x10, 0, 0, 0, 1]), 0x10, "nonzero mask"),
    ],
)
def test_decode_rejects_malformed_replies(raw: bytes, seq: int, why: str) -> None:
    with pytest.raises(wire.ReplyError, match=why):
        wire.decode_reply(raw, seq)


def test_encode_reply_zeroes_the_mask_on_error() -> None:
    assert wire.encode_reply(p.STATUS_INTERNAL, 3, 0xFFFFFFFF) == bytes(
        [3, 3, 0, 0, 0, 0]
    )
    assert wire.encode_reply(p.STATUS_OK, 3, 0x12345678)[2:] == bytes.fromhex(
        "12345678"
    )


def test_seq_wraps_at_256() -> None:
    assert wire.next_seq(0) == 1
    assert wire.next_seq(0xFE) == 0xFF
    assert wire.next_seq(0xFF) == 0


def test_encode_frame_refuses_malformed_batches() -> None:
    boxes = list(batches.NOTEBOOK_32)
    with pytest.raises(ValueError, match="exactly"):
        wire.encode_frame(boxes[:-1], 0, 0)
    with pytest.raises(ValueError, match="seq"):
        wire.encode_frame(boxes, 0, 256)
    with pytest.raises(ValueError, match="present_mask"):
        wire.encode_frame(boxes, 1 << p.N, 0)


def test_latency_timer_path_resolves_symlinks(tmp_path) -> None:  # noqa: ANN001
    real = tmp_path / "ttyUSB1"
    real.touch()
    link = tmp_path / "usb-Digilent_Basys3-if01-port0"
    link.symlink_to(real)
    path = host.latency_timer_path(str(link))
    assert path == host.Path("/sys/bus/usb-serial/devices/ttyUSB1/latency_timer")


def test_missing_port_exits_2_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = host.main(["--port", "/dev/does-not-exist-nms", "--selftest"])
    assert code == host.EXIT_USAGE
    assert "cannot open /dev/does-not-exist-nms" in capsys.readouterr().out


def test_no_mode_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert host.main(["--port", "/dev/does-not-exist-nms"]) == host.EXIT_USAGE
    assert "choose at least one" in capsys.readouterr().out
