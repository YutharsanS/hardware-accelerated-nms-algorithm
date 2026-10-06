"""Tests for the pipelined host path, against a byte-level fake of the board."""

from __future__ import annotations

import random

import pytest

from models.nms import batches, host, model, wire
from models.nms import params as p


class LoopbackSerial:
    """Parses whatever frames are written and answers each as the board would."""

    def __init__(self, *, drop: int = 0) -> None:
        self.pending = b""
        self.drop = drop
        self.writes = 0

    def reset_input_buffer(self) -> None:
        self.pending = b""

    def write(self, data: bytes) -> None:
        self.writes += 1
        for i in range(0, len(data), p.FRAME_BYTES_IN):
            frame = data[i : i + p.FRAME_BYTES_IN]
            body = frame[len(p.MAGIC) : -1]
            records = body[: p.N * p.RECORD_BYTES]
            boxes = [
                model.unpack_record(int.from_bytes(records[j : j + 8], "big"))
                for j in range(0, len(records), p.RECORD_BYTES)
            ]
            present = int.from_bytes(body[-5:-1], "big")
            seq = body[-1]
            keep = model.nms_sequential(boxes, present)
            self.pending += wire.encode_reply(p.STATUS_OK, seq, keep)

    def flush(self) -> None:
        pass

    def read(self, n: int) -> bytes:
        out, self.pending = self.pending[: n - self.drop], self.pending[n:]
        return out


def fake_board(serial: LoopbackSerial) -> host.Board:
    board = object.__new__(host.Board)
    board._ser = serial
    board.seq = 254
    return board


def test_transact_many_answers_every_batch_in_one_write() -> None:
    rng = random.Random(7)
    sent = [(batch, rng.getrandbits(p.N)) for batch in batches.hostile_stream(5)]
    serial = LoopbackSerial()
    board = fake_board(serial)
    replies = board.transact_many(sent)
    assert serial.writes == 1
    assert [r.seq for r in replies] == [254, 255, 0, 1, 2]
    assert board.seq == 3
    for (boxes, present), reply in zip(sent, replies, strict=True):
        assert reply.ok
        assert reply.keep_mask == model.nms_sequential(boxes, present)


def test_transact_many_empty_sends_nothing() -> None:
    serial = LoopbackSerial()
    assert fake_board(serial).transact_many([]) == []
    assert serial.writes == 0


def test_transact_many_short_read_times_out() -> None:
    board = fake_board(LoopbackSerial(drop=1))
    with pytest.raises(host.ReplyTimeout, match="11 of 12"):
        board.transact_many([(batches.case_disjoint(), (1 << p.N) - 1)] * 2)
