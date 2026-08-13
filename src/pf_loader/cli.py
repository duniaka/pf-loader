import string
import sys
from typing import NamedTuple

import click
import enlighten

from pf_loader.pcsclite_backend import DEFAULT_PCSC_LIB, RealPCSC
from pf_loader.pyscard_backend import PyscardPCSC

SCARD_RESET_CARD = 0x0001
SCARD_UNPOWER_CARD = 0x0002


def parse_hex(s):
    out = []
    for tok in s.split():
        if tok.upper() == "XX":
            out.append(None)
        elif (
            len(tok) > 2
            and len(tok) % 2 == 0
            and all(c in string.hexdigits for c in tok)
        ):
            out += [int(tok[i : i + 2], 16) for i in range(0, len(tok), 2)]
        else:
            v = int(tok, 16)
            if not 0 <= v <= 255:
                raise ValueError(f"byte out of range: {tok}")
            out.append(v)
    return out


def parse_send(s):
    vals = parse_hex(s)
    if any(v is None for v in vals):
        raise ValueError("XX wildcard not allowed in a `>` send line")
    return bytes(vals)


def matches(expected, actual):
    return len(expected) == len(actual) and all(
        e is None or e == a for e, a in zip(expected, actual)
    )


def fmt(bs):
    return " ".join(f"{b:02X}" for b in bs)


def fmt_expected(expected):
    return " ".join("XX" if e is None else f"{e:02X}" for e in expected)


def count_sends(scripts):
    return sum(1 for _n, lines in scripts for ln in lines if ln.strip().startswith(">"))


class Send(NamedTuple):
    apdu: bytes


class Reset(NamedTuple):
    cold: bool


class Emit(NamedTuple):
    text: str


class Progress(NamedTuple):
    done: int
    total: int
    stage: str


def interpret(scripts, total):
    done = 0
    stage = ""
    last = None

    for name, lines in scripts:
        for raw in lines:
            line = raw.rstrip("\n")
            s = line.strip()
            if s.startswith(">"):
                last = yield Send(parse_send(s[1:]))
                done += 1
                yield Emit(line)
                yield Progress(done, total, stage)
            elif s.startswith("<"):
                expected = parse_hex(s[1:])
                actual = last if last is not None else b""
                yield Emit("<" + fmt(actual))
                if last is None or not matches(expected, actual):
                    yield Emit(
                        f"FAILED: expected '{fmt_expected(expected)}', "
                        f"got '{fmt(actual)}'. Aborted."
                    )
                    return 1
            elif s == "WARM":
                yield Reset(cold=False)
                yield Emit(line)
            elif s == "COLD":
                yield Reset(cold=True)
                yield Emit(line)
            elif s.startswith(":"):
                stage = s
                yield Emit(line)
                yield Progress(done, total, stage)
            elif s == "" or s.startswith("#"):
                yield Emit(line)
            else:
                raise ValueError(f"{name}: unrecognized line: {line!r}")

    return 0


def run(scripts, card, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    total = count_sends(scripts)
    gen = interpret(scripts, total)
    reply = None
    # enlighten pins the counter to the bottom row and scrolls the transcript above it; on a
    # non-tty stream it auto-disables, so piped stdout stays clean.
    manager = enlighten.get_manager(stream=err)
    bar = manager.counter(total=total, desc="", unit="cmd", color="green", leave=False)
    try:
        while True:
            eff = gen.send(reply)
            reply = None
            if isinstance(eff, Send):
                reply = card.send(eff.apdu)
            elif isinstance(eff, Reset):
                card.cold() if eff.cold else card.warm()
            elif isinstance(eff, Emit):
                print(eff.text, file=out)
            elif isinstance(eff, Progress):
                bar.desc = eff.stage
                bar.count = eff.done
                bar.refresh()
    except StopIteration as stop:
        return stop.value
    finally:
        bar.close()
        manager.stop()


class Card:
    def __init__(self, p, handle, proto):
        self.p, self.handle, self.proto = p, handle, proto

    def send(self, apdu):
        return self.p.transmit(self.handle, self.proto, bytes(apdu))

    def warm(self):
        self.proto = self.p.reconnect(self.handle, disposition=SCARD_RESET_CARD)

    def cold(self):
        self.proto = self.p.reconnect(self.handle, disposition=SCARD_UNPOWER_CARD)


def _pick_reader(readers, sel):
    if sel is not None and sel.isdigit():
        idx = int(sel)
        if idx >= len(readers):
            raise SystemExit(f"reader index {idx} out of range, see --list-readers")
        return readers[idx]
    if sel:
        matches_ = [r for r in readers if sel.lower() in r.lower()]
        if not matches_:
            raise SystemExit(f"no reader matching {sel!r}, see --list-readers")
        return matches_[0]
    return next((r for r in readers if "virtual pcd" not in r.lower()), readers[0])


@click.command()
@click.option(
    "-s",
    "--script",
    "scripts",
    multiple=True,
    type=click.File("r"),
    help="a .pf script to run (repeatable; run in order, shared card session)",
)
@click.option(
    "--reader", help="reader index or name substring (default: first non-virtual)"
)
@click.option(
    "--pcsc-lib",
    is_flag=False,
    flag_value="",
    default=None,
    envvar="PCSC_LIB",
    help="use the direct ctypes bridge instead of pyscard, binding this pcsc-lite "
    f"library. Default: {DEFAULT_PCSC_LIB}",
)
@click.option("--list-readers", is_flag=True, help="list PC/SC readers and exit")
def main(scripts, reader, pcsc_lib, list_readers):
    p = RealPCSC(pcsc_lib) if pcsc_lib is not None else PyscardPCSC()
    readers = p.list_readers()
    if not readers:
        raise SystemExit("no PC/SC reader found")

    if list_readers:
        for i, r in enumerate(readers):
            print(f"{i}: {r}")
        p.release()
        return

    if not scripts:
        raise SystemExit("nothing to do: pass -s/--script FILE (or --list-readers)")

    reader = _pick_reader(readers, reader)
    print(f"reader: {reader}", file=sys.stderr)
    handle, proto = p.connect(reader)
    print(f"ATR: {p.get_atr(handle).hex(' ')}", file=sys.stderr)

    loaded = [(f.name, f.read().splitlines()) for f in scripts]
    try:
        code = run(loaded, Card(p, handle, proto))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        code = 2
    finally:
        p.disconnect(handle)
        p.release()
    sys.exit(code)


if __name__ == "__main__":
    main()
