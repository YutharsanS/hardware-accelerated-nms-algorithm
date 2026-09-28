"""Host program for the NMS accelerator on a Basys 3: send batches, check the replies.

Run it on the PC connected to the board's micro-USB port. The FT2232HQ there enumerates two
serial devices: interface A is JTAG (Vivado programs the FPGA through it) and interface B is
the UART that ``nms_top`` listens on -- normally ``/dev/ttyUSB1``. Program the board first
(``make program``), then, in bring-up order::

    uv run python -m models.nms.host --selftest       # the 20 committed frames, byte-exact
    uv run python -m models.nms.host --crc-test       # a corrupted frame must be rejected
    uv run python -m models.nms.host --random 1000    # hostile batches vs the golden model
    uv run python -m models.nms.host --latency 500    # round-trip time distribution

Exit status: 0 when every check passed, 1 when any failed or timed out, 2 when the port could
not be opened or no mode was given.

The FTDI driver's latency timer defaults to 16 ms and holds a short read -- exactly the
6-byte reply -- for that long. At startup the host sets it to 1 ms (docs/plan.md P5), or
prints the ``sudo`` command when it lacks permission. It resets whenever the board is
re-plugged, which is why the host checks it every run rather than trusting a one-time fix.
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Self

from models.nms import batches, model, vectors, wire
from models.nms import params as p

DEFAULT_PORT = "/dev/ttyUSB1"
DEFAULT_TIMEOUT_S = 0.5
"""A reply takes 2.70 ms of wire time at 1 Mbaud, plus up to 16 ms of FTDI latency timer."""

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


class PortError(OSError):
    """The serial port could not be opened."""


class ReplyTimeout(TimeoutError):
    """Fewer than six reply bytes arrived before the timeout."""


class Board:
    """The accelerator at the end of a serial port."""

    def __init__(
        self, port: str, baud: int = p.BAUD, timeout: float = DEFAULT_TIMEOUT_S
    ) -> None:
        """Open the port.

        Args:
            port: Serial device, e.g. ``/dev/ttyUSB1``.
            baud: Line rate; ``nms_top`` is built for ``params.BAUD``.
            timeout: Seconds to wait for a complete reply.

        Raises:
            PortError: If the port cannot be opened.
        """
        import serial  # only the board path needs pyserial, so tests never import it

        try:
            self._ser = serial.Serial(
                port,
                baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
            )
        except (serial.SerialException, OSError) as exc:
            msg = f"cannot open {port}: {exc}"
            raise PortError(msg) from exc
        self.seq = 0

    def __enter__(self) -> Self:
        """Enter a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the port on leaving a ``with`` block."""
        self._ser.close()

    def send_frame(self, frame: bytes, seq: int) -> wire.Reply:
        """Send one ready-made frame and return its validated reply.

        Stale input is flushed first, so a late reply to an earlier timed-out frame cannot be
        read as this frame's answer; the seq check in :func:`wire.decode_reply` backs that up.

        Args:
            frame: A complete 264-byte frame.
            seq: The seq byte inside that frame.

        Returns:
            The decoded reply.

        Raises:
            ReplyTimeout: If the reply did not arrive in full.
        """
        self._ser.reset_input_buffer()
        self._ser.write(frame)
        self._ser.flush()
        raw = self._ser.read(p.REPLY_BYTES)
        if len(raw) < p.REPLY_BYTES:
            msg = f"only {len(raw)} of {p.REPLY_BYTES} reply bytes before the timeout"
            raise ReplyTimeout(msg)
        return wire.decode_reply(raw, seq)

    def transact(self, boxes: list[model.Box], present_mask: int) -> wire.Reply:
        """Send a batch with the next sequence number and return the reply.

        Args:
            boxes: The 32 boxes, indexed by slot.
            present_mask: Which slots hold real detections.

        Returns:
            The decoded reply.
        """
        seq = self.seq
        self.seq = wire.next_seq(seq)
        return self.send_frame(wire.encode_frame(boxes, present_mask, seq), seq)


# --- the FTDI latency timer ---------------------------------------------------------


def latency_timer_path(port: str) -> Path:
    """Return the sysfs file holding the FTDI latency timer for a port.

    Args:
        port: The serial device. A ``/dev/serial/by-id/...`` symlink is resolved first,
            since sysfs knows the device only by its ``ttyUSBn`` name.

    Returns:
        ``/sys/bus/usb-serial/devices/ttyUSBn/latency_timer``.
    """
    name = Path(os.path.realpath(port)).name
    return Path("/sys/bus/usb-serial/devices") / name / "latency_timer"


def set_latency_timer(port: str, ms: int = 1) -> tuple[int | None, str]:
    """Set the latency timer, reporting what happened.

    Args:
        port: The serial device.
        ms: The value to set.

    Returns:
        ``(value now in effect or None if unreadable, a line to print)``.
    """
    path = latency_timer_path(port)
    try:
        before = int(path.read_text())
    except OSError:
        return None, f"latency timer: {path} not readable (not an FTDI port?)"
    if before == ms:
        return before, f"latency timer: {before} ms"
    try:
        path.write_text(f"{ms}\n")
    except OSError:
        return before, (
            f"latency timer: {before} ms -- could not set it to {ms} ms without permission.\n"
            f"  run: echo {ms} | sudo tee {path}"
        )
    return int(path.read_text()), f"latency timer: {before} ms -> {ms} ms"


# --- the modes ------------------------------------------------------------------------


def run_selftest(board: Board) -> bool:
    """Send every committed frame verbatim and compare each reply byte for byte.

    Args:
        board: The connected board.

    Returns:
        Whether all frames were answered exactly as the golden model expects.
    """
    names = vectors.read_manifest()
    frames = vectors.read_frames()
    passed = 0
    for name, (frame, expected) in zip(names, frames, strict=True):
        seq = frame[p.FRAME_BYTES_IN - 2]
        want = wire.decode_reply(expected, seq)
        try:
            got = board.send_frame(frame, seq)
        except (ReplyTimeout, wire.ReplyError) as exc:
            print(f"  FAIL {name:<16} {exc}")
            continue
        if got == want:
            passed += 1
            print(f"  pass {name:<16} keep_mask {got.keep_mask:#010x}")
        else:
            print(
                f"  FAIL {name:<16} status {got.status:#04x} keep_mask {got.keep_mask:#010x}"
                f", expected status {want.status:#04x} keep_mask {want.keep_mask:#010x}"
            )
    print(f"selftest: {passed}/{len(frames)} frames answered exactly")
    return passed == len(frames)


def run_crc_test(board: Board) -> bool:
    """Send a frame with one corrupted payload byte; it must be rejected, not computed.

    Args:
        board: The connected board.

    Returns:
        Whether the board replied status 0x01 with a zero mask.
    """
    frame = bytearray(vectors.read_frames()[0][0])
    frame[100] ^= 0x10
    seq = frame[p.FRAME_BYTES_IN - 2]
    try:
        got = board.send_frame(bytes(frame), seq)
    except (ReplyTimeout, wire.ReplyError) as exc:
        print(f"crc-test: FAIL -- {exc}")
        return False
    ok = got.status == p.STATUS_CRC_FAIL
    print(
        f"crc-test: {'pass' if ok else 'FAIL'} -- status {got.status:#04x}"
        f" (expected {p.STATUS_CRC_FAIL:#04x}), mask {got.keep_mask:#010x}"
    )
    return ok


def run_random(board: Board, count: int, seed: int) -> bool:
    """Send hostile random batches and check each against both model forms.

    Three batches in four have every slot present; the rest draw a random present_mask.

    Args:
        board: The connected board.
        count: Number of batches.
        seed: Seed for the geometry and the masks.

    Returns:
        Whether every reply matched the golden model.
    """
    rng = random.Random(seed)
    failures = 0
    for i, boxes in enumerate(batches.hostile_stream(count, seed=seed)):
        mask = rng.getrandbits(p.N) if i % 4 == 3 else (1 << p.N) - 1
        want = model.nms_allpairs(boxes, mask)
        assert want == model.nms_sequential(boxes, mask), "the two model forms disagree"
        try:
            got = board.transact(boxes, mask)
        except (ReplyTimeout, wire.ReplyError) as exc:
            failures += 1
            print(f"  FAIL batch {i}: {exc}")
            continue
        if not got.ok or got.keep_mask != want:
            failures += 1
            print(
                f"  FAIL batch {i}: status {got.status:#04x} keep_mask {got.keep_mask:#010x}"
                f", model says {want:#010x}"
            )
    print(
        f"random: {count - failures}/{count} batches bit-exact against the golden model"
    )
    return failures == 0


def run_latency(board: Board, count: int, timer: int | None) -> bool:
    """Time round trips of one fixed batch and print the distribution.

    Args:
        board: The connected board.
        count: Number of round trips.
        timer: The FTDI latency timer in effect, for the report.

    Returns:
        Whether every round trip completed correctly.
    """
    boxes = list(batches.NOTEBOOK_32)
    mask = (1 << p.N) - 1
    want = model.nms_allpairs(boxes, mask)
    times_ms: list[float] = []
    for _ in range(count):
        start = time.perf_counter()
        try:
            got = board.transact(boxes, mask)
        except (ReplyTimeout, wire.ReplyError) as exc:
            print(f"latency: FAIL -- {exc}")
            return False
        times_ms.append((time.perf_counter() - start) * 1e3)
        if got.keep_mask != want:
            print(f"latency: FAIL -- wrong keep_mask {got.keep_mask:#010x}")
            return False
    times_ms.sort()
    p99 = times_ms[min(len(times_ms) - 1, round(0.99 * (len(times_ms) - 1)))]
    timer_txt = f"{timer} ms" if timer is not None else "unknown"
    print(
        f"latency: {count} round trips, latency timer {timer_txt}: "
        f"min {times_ms[0]:.3f} ms, median {statistics.median(times_ms):.3f} ms, "
        f"p99 {p99:.3f} ms, max {times_ms[-1]:.3f} ms "
        f"(wire time alone is {p.FRAME_BYTES_IN * 10 / p.BAUD * 1e3 + 0.06:.2f} ms)"
    )
    return True


# --- CLI ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, open the board, and run the requested modes in bring-up order.

    Args:
        argv: Command-line arguments, excluding the program name.

    Returns:
        The process exit status.
    """
    parser = argparse.ArgumentParser(
        prog="python -m models.nms.host",
        description="Drive the NMS accelerator on a Basys 3 and check it against the model.",
    )
    parser.add_argument(
        "--port", default=DEFAULT_PORT, help="UART device (default %(default)s)"
    )
    parser.add_argument(
        "--baud", type=int, default=p.BAUD, help="line rate (default %(default)s)"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="reply timeout in s"
    )
    parser.add_argument(
        "--selftest", action="store_true", help="send the 20 committed frames"
    )
    parser.add_argument(
        "--crc-test", action="store_true", help="send a corrupted frame"
    )
    parser.add_argument(
        "--random", type=int, metavar="N", help="send N hostile batches"
    )
    parser.add_argument("--seed", type=int, default=2026, help="seed for --random")
    parser.add_argument("--latency", type=int, metavar="N", help="time N round trips")
    parser.add_argument(
        "--leave-latency-timer",
        action="store_true",
        help="do not set the FTDI latency timer to 1 ms (to measure the 16 ms default)",
    )
    args = parser.parse_args(argv)

    if not (args.selftest or args.crc_test or args.random or args.latency):
        parser.print_usage()
        print("choose at least one of --selftest, --crc-test, --random N, --latency N")
        return EXIT_USAGE

    if args.leave_latency_timer:
        timer, line = None, "latency timer: left unchanged"
        try:
            timer = int(latency_timer_path(args.port).read_text())
            line = f"latency timer: left at {timer} ms"
        except OSError:
            pass
    else:
        timer, line = set_latency_timer(args.port)
    print(line)

    try:
        board = Board(args.port, args.baud, args.timeout)
    except PortError as exc:
        print(exc)
        return EXIT_USAGE

    ok = True
    with board:
        if args.selftest:
            ok &= run_selftest(board)
        if args.crc_test:
            ok &= run_crc_test(board)
        if args.random:
            ok &= run_random(board, args.random, args.seed)
        if args.latency:
            ok &= run_latency(board, args.latency, timer)
    print("ALL PASSED" if ok else "FAILED")
    return EXIT_OK if ok else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
