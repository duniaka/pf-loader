import ctypes
import sys

DEFAULT_PCSC_LIB = (
    "/opt/homebrew/opt/pcsc-lite/lib/libpcsclite_real.dylib"
    if sys.platform == "darwin"
    else "libpcsclite.so.1"
)

SCARD_LEAVE_CARD = 0x0000
SCARD_SCOPE_SYSTEM = 0x0002
SCARD_SHARE_EXCLUSIVE = 0x0001
SCARD_PROTOCOL_ANY = 0x0003  # T=0 | T=1
SCARD_AUTOALLOCATE = ctypes.c_uint32(-1).value
SCARD_S_SUCCESS = 0

LONG = ctypes.c_int32
DWORD = ctypes.c_uint32


class SCardIoRequest(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("dwProtocol", ctypes.c_ulong), ("cbPciLength", ctypes.c_ulong)]


class PCSCError(Exception):
    def __init__(self, what, rv):
        self.rv = rv & 0xFFFFFFFF
        super().__init__(f"{what} failed: 0x{self.rv:08X}")


class RealPCSC:
    def __init__(self, lib=None):
        self._lib = ctypes.CDLL(lib or DEFAULT_PCSC_LIB)
        self._ctx = LONG()
        self._check(
            self._lib.SCardEstablishContext(
                DWORD(SCARD_SCOPE_SYSTEM), None, None, ctypes.byref(self._ctx)
            ),
            "SCardEstablishContext",
        )

    @staticmethod
    def _check(rv, what):
        if rv != SCARD_S_SUCCESS:
            raise PCSCError(what, rv)

    def list_readers(self):
        msz = ctypes.c_char_p()
        pcch = DWORD(SCARD_AUTOALLOCATE)
        rv = self._lib.SCardListReaders(
            self._ctx, None, ctypes.byref(msz), ctypes.byref(pcch)
        )
        if rv != SCARD_S_SUCCESS:
            return []
        raw = ctypes.string_at(msz, pcch.value)
        return [n.decode() for n in raw.split(b"\x00") if n]

    def connect(self, reader, protocol=SCARD_PROTOCOL_ANY):
        handle, active = LONG(), DWORD()
        self._check(
            self._lib.SCardConnect(
                self._ctx,
                reader.encode(),
                DWORD(SCARD_SHARE_EXCLUSIVE),
                DWORD(protocol),
                ctypes.byref(handle),
                ctypes.byref(active),
            ),
            "SCardConnect",
        )
        return handle.value, active.value

    def reconnect(
        self, handle, protocol=SCARD_PROTOCOL_ANY, disposition=SCARD_LEAVE_CARD
    ):
        active = DWORD()
        self._check(
            self._lib.SCardReconnect(
                LONG(handle),
                DWORD(SCARD_SHARE_EXCLUSIVE),
                DWORD(protocol),
                DWORD(disposition),
                ctypes.byref(active),
            ),
            "SCardReconnect",
        )
        return active.value

    def disconnect(self, handle, disposition=SCARD_LEAVE_CARD):
        self._lib.SCardDisconnect(LONG(handle), DWORD(disposition))

    def get_atr(self, handle):
        reader_buf = ctypes.create_string_buffer(128)
        reader_len = DWORD(128)
        state, proto = DWORD(), DWORD()
        atr = (ctypes.c_ubyte * 33)()
        atr_len = DWORD(33)
        self._check(
            self._lib.SCardStatus(
                LONG(handle),
                reader_buf,
                ctypes.byref(reader_len),
                ctypes.byref(state),
                ctypes.byref(proto),
                atr,
                ctypes.byref(atr_len),
            ),
            "SCardStatus",
        )
        return bytes(atr[: atr_len.value])

    def transmit(self, handle, protocol, data):
        send_pci = SCardIoRequest(protocol, ctypes.sizeof(SCardIoRequest))
        recv_buf = ctypes.create_string_buffer(258 + 2)
        recv_len = DWORD(len(recv_buf))
        self._check(
            self._lib.SCardTransmit(
                LONG(handle),
                ctypes.byref(send_pci),
                bytes(data),
                DWORD(len(data)),
                None,
                recv_buf,
                ctypes.byref(recv_len),
            ),
            "SCardTransmit",
        )
        return recv_buf.raw[: recv_len.value]

    def release(self):
        self._lib.SCardReleaseContext(self._ctx)
