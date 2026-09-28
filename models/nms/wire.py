"""The host <-> FPGA wire format: one encoder, one decoder, shared by everything.

``docs/architecture.md`` section 3 is normative. Every multi-byte field goes most-significant
byte first::

    host -> FPGA  A5 5A | 32 records x 8 B | present_mask 4 B | seq 1 B | crc8 1 B   (264 B)
    FPGA -> host  status 1 B | seq 1 B | keep_mask 4 B                                  (6 B)

Both the committed ``frames.txt`` (through :func:`models.nms.vectors.build_frame`) and the
live host (:mod:`models.nms.host`) build frames with :func:`encode_frame`, so the frames the
testbenches were verified against and the frames sent to the board cannot differ.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.nms import model
from models.nms import params as p

MASK_BYTES = p.N // 8


class ReplyError(ValueError):
    """A reply that violates the wire format: wrong length, wrong seq, or a leaked mask."""


@dataclass(frozen=True)
class Reply:
    """A decoded FPGA reply.

    Attributes:
        status: ``STATUS_OK``, ``STATUS_CRC_FAIL``, ``STATUS_BUSY`` or ``STATUS_INTERNAL``.
        seq: The echoed sequence number.
        keep_mask: Bit *i* set means the *i*-th box sent survived. Zero unless status is OK.
    """

    status: int
    seq: int
    keep_mask: int

    @property
    def ok(self) -> bool:
        """Whether the batch was computed."""
        return self.status == p.STATUS_OK


def encode_frame(boxes: list[model.Box], present_mask: int, seq: int) -> bytes:
    """Build the 264-byte host -> FPGA frame.

    Args:
        boxes: Exactly ``N`` boxes, indexed by slot. Absent slots still need a box; its
            contents are ignored because ``present_mask`` marks it absent.
        present_mask: Bit *i* set means slot *i* holds a real detection.
        seq: Frame sequence number, 0..255, echoed in the reply.

    Returns:
        The frame, magic first and CRC-8 last.

    Raises:
        ValueError: If the batch is the wrong size or a field is out of range.
    """
    if len(boxes) != p.N:
        msg = f"a frame carries exactly {p.N} boxes, got {len(boxes)}"
        raise ValueError(msg)
    if not 0 <= present_mask < (1 << p.N):
        msg = f"present_mask {present_mask:#x} does not fit {p.N} bits"
        raise ValueError(msg)
    if not 0 <= seq <= 0xFF:
        msg = f"seq {seq} does not fit one byte"
        raise ValueError(msg)
    body = b"".join(model.pack_record(b).to_bytes(p.RECORD_BYTES, "big") for b in boxes)
    body += present_mask.to_bytes(MASK_BYTES, "big") + bytes([seq])
    return bytes(p.MAGIC) + body + bytes([p.crc8(body)])


def encode_reply(status: int, seq: int, keep_mask: int) -> bytes:
    """Build the 6-byte FPGA -> host reply, applying the zero-mask-on-error rule.

    Args:
        status: The status byte.
        seq: The echoed sequence number.
        keep_mask: The survivors; dropped to zero unless ``status`` is OK.

    Returns:
        The reply bytes.
    """
    mask = keep_mask if status == p.STATUS_OK else 0
    return bytes([status, seq]) + mask.to_bytes(MASK_BYTES, "big")


def decode_reply(raw: bytes, expected_seq: int) -> Reply:
    """Decode and validate a 6-byte reply.

    Args:
        raw: The bytes read from the port.
        expected_seq: The seq of the frame this reply should answer.

    Returns:
        The decoded reply.

    Raises:
        ReplyError: If the reply is short, answers a different frame, carries an unknown
            status, or leaks a mask alongside an error status.
    """
    if len(raw) != p.REPLY_BYTES:
        msg = f"reply is {len(raw)} bytes, expected {p.REPLY_BYTES}"
        raise ReplyError(msg)
    status, seq = raw[0], raw[1]
    keep_mask = int.from_bytes(raw[2:], "big")
    if seq != expected_seq:
        msg = f"reply answers seq {seq:#04x}, expected {expected_seq:#04x}"
        raise ReplyError(msg)
    known = {p.STATUS_OK, p.STATUS_CRC_FAIL, p.STATUS_BUSY, p.STATUS_INTERNAL}
    if status not in known:
        msg = f"unknown status {status:#04x}"
        raise ReplyError(msg)
    if status != p.STATUS_OK and keep_mask != 0:
        msg = f"status {status:#04x} arrived with a nonzero mask {keep_mask:#010x}"
        raise ReplyError(msg)
    return Reply(status=status, seq=seq, keep_mask=keep_mask)


def next_seq(seq: int) -> int:
    """Return the sequence number after ``seq``, wrapping at 256.

    Args:
        seq: The current sequence number.

    Returns:
        ``(seq + 1) mod 256``.
    """
    return (seq + 1) & 0xFF
