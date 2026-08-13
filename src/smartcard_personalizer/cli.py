#!/usr/bin/env python3
import argparse
import ctypes
import os
import string
import sys
import time
from typing import NamedTuple

import enlighten

SCARD_RESET_CARD = 0x0001
SCARD_UNPOWER_CARD = 0x0002

# --- Minimal ctypes bridge to the real pcsc-lite client library ---------------
# Self-contained port of the calls pf_loader uses, so we bind whichever pcscd is
# actually running instead of side-loading pysim's (now-patched) _realpcsc.py.
# pyscard's macOS build dlopen()s Apple's PCSC.framework unconditionally and its
# compiled SCardTransmit path only lines up against Apple's ABI, so we drive the
# real client lib directly. Point at it with --pcsc-lib or PCSC_LIB.
DEFAULT_PCSC_LIB = ("/opt/homebrew/opt/pcsc-lite/lib/libpcsclite_real.dylib"
                    if sys.platform == "darwin" else "libpcsclite.so.1")

SCARD_SCOPE_SYSTEM = 0x0002
SCARD_SHARE_EXCLUSIVE = 0x0001
SCARD_PROTOCOL_ANY = 0x0003  # T=0 | T=1
SCARD_LEAVE_CARD = 0x0000
SCARD_AUTOALLOCATE = ctypes.c_uint32(-1).value
SCARD_S_SUCCESS = 0

LONG = ctypes.c_int32
DWORD = ctypes.c_uint32


class SCardIoRequest(ctypes.Structure):
    # PCSC/pcsclite.h SCARD_IO_REQUEST under #pragma pack(1): two "unsigned long"
    # fields (8 bytes each on LP64), NOT DWORD-sized. Load-bearing ABI detail.
    _pack_ = 1
    _fields_ = [("dwProtocol", ctypes.c_ulong), ("cbPciLength", ctypes.c_ulong)]


class PCSCError(Exception):
    def __init__(self, what, rv):
        self.rv = rv & 0xFFFFFFFF
        super().__init__(f"{what} failed: 0x{self.rv:08X}")


class RealPCSC:
    """Thin ctypes wrapper around the real pcsc-lite client library."""

    def __init__(self, lib=None):
        self._lib = ctypes.CDLL(lib or os.environ.get("PCSC_LIB") or DEFAULT_PCSC_LIB)
        self._ctx = LONG()
        self._check(self._lib.SCardEstablishContext(
            DWORD(SCARD_SCOPE_SYSTEM), None, None, ctypes.byref(self._ctx)),
            "SCardEstablishContext")

    @staticmethod
    def _check(rv, what):
        if rv != SCARD_S_SUCCESS:
            raise PCSCError(what, rv)

    def list_readers(self):
        msz = ctypes.c_char_p()
        pcch = DWORD(SCARD_AUTOALLOCATE)
        rv = self._lib.SCardListReaders(self._ctx, None, ctypes.byref(msz), ctypes.byref(pcch))
        if rv != SCARD_S_SUCCESS:
            return []
        raw = ctypes.string_at(msz, pcch.value)
        return [n.decode() for n in raw.split(b"\x00") if n]

    def connect(self, reader, protocol=SCARD_PROTOCOL_ANY):
        handle, active = LONG(), DWORD()
        self._check(self._lib.SCardConnect(self._ctx, reader.encode(),
                                            DWORD(SCARD_SHARE_EXCLUSIVE), DWORD(protocol),
                                            ctypes.byref(handle), ctypes.byref(active)),
                    "SCardConnect")
        return handle.value, active.value

    def reconnect(self, handle, protocol=SCARD_PROTOCOL_ANY, disposition=SCARD_LEAVE_CARD):
        active = DWORD()
        self._check(self._lib.SCardReconnect(LONG(handle), DWORD(SCARD_SHARE_EXCLUSIVE),
                                              DWORD(protocol), DWORD(disposition),
                                              ctypes.byref(active)), "SCardReconnect")
        return active.value

    def disconnect(self, handle, disposition=SCARD_LEAVE_CARD):
        self._lib.SCardDisconnect(LONG(handle), DWORD(disposition))

    def get_atr(self, handle):
        reader_buf = ctypes.create_string_buffer(128)
        reader_len = DWORD(128)
        state, proto = DWORD(), DWORD()
        atr = (ctypes.c_ubyte * 33)()
        atr_len = DWORD(33)
        self._check(self._lib.SCardStatus(LONG(handle), reader_buf, ctypes.byref(reader_len),
                                           ctypes.byref(state), ctypes.byref(proto),
                                           atr, ctypes.byref(atr_len)), "SCardStatus")
        return bytes(atr[:atr_len.value])

    def transmit(self, handle, protocol, data):
        send_pci = SCardIoRequest(protocol, ctypes.sizeof(SCardIoRequest))
        recv_buf = ctypes.create_string_buffer(258 + 2)
        recv_len = DWORD(len(recv_buf))
        self._check(self._lib.SCardTransmit(LONG(handle), ctypes.byref(send_pci),
                                             bytes(data), DWORD(len(data)), None,
                                             recv_buf, ctypes.byref(recv_len)), "SCardTransmit")
        return recv_buf.raw[:recv_len.value]

    def release(self):
        self._lib.SCardReleaseContext(self._ctx)


def parse_hex(s):
    out = []
    for tok in s.split():
        if tok.upper() == "XX":
            out.append(None)
        elif len(tok) > 2 and len(tok) % 2 == 0 and all(c in string.hexdigits for c in tok):
            out += [int(tok[i:i + 2], 16) for i in range(0, len(tok), 2)]
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
        e is None or e == a for e, a in zip(expected, actual))


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


def interpret(scripts):
    total = count_sends(scripts)
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
                    yield Emit(f"FAILED: expected '{fmt_expected(expected)}', "
                               f"got '{fmt(actual)}'. Aborted.")
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
    gen = interpret(scripts)
    reply = None
    # enlighten pins the counter to the bottom row and scrolls the transcript above it; on a
    # non-tty stream it auto-disables, so piped stdout stays clean.
    manager = enlighten.get_manager(stream=err)
    bar = manager.counter(total=count_sends(scripts), desc="", unit="cmd", color="green",
                          leave=False)
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
        time.sleep(1)
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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--script", action="append", default=[], metavar="FILE",
                    help="a .pf script to run (repeatable; run in order, shared card session)")
    ap.add_argument("--reader", help="reader index or name substring (default: first non-virtual)")
    ap.add_argument("--pcsc-lib", help="path to the pcsc-lite client library "
                    f"(default: $PCSC_LIB or {DEFAULT_PCSC_LIB})")
    ap.add_argument("--list-readers", action="store_true", help="list PC/SC readers and exit")
    args = ap.parse_args(argv)

    p = RealPCSC(args.pcsc_lib)
    readers = p.list_readers()
    if not readers:
        raise SystemExit("no PC/SC reader found")

    if args.list_readers:
        for i, r in enumerate(readers):
            print(f"{i}: {r}")
        p.release()
        return 0

    if not args.script:
        raise SystemExit("nothing to do: pass -s/--script FILE (or --list-readers)")

    reader = _pick_reader(readers, args.reader)
    print(f"reader: {reader}", file=sys.stderr)
    handle, proto = p.connect(reader)
    print(f"ATR: {p.get_atr(handle).hex(' ')}", file=sys.stderr)

    scripts = [(f, open(f).read().splitlines()) for f in args.script]
    try:
        code = run(scripts, Card(p, handle, proto))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        code = 2
    finally:
        p.disconnect(handle)
        p.release()
    return code


if __name__ == "__main__":
    sys.exit(main())
