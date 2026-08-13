SCARD_LEAVE_CARD = 0x0000


class PyscardPCSC:
    def __init__(self):
        from smartcard.System import readers

        self._readers = readers

    def list_readers(self):
        self._cached_readers = self._readers()
        return [str(r) for r in self._cached_readers]

    def _find(self, reader):
        r = next((x for x in self._cached_readers if str(x) == reader), None)
        if r is None:
            raise SystemExit(f"reader vanished: {reader!r}")
        return r

    def connect(self, reader):
        conn = self._find(reader).createConnection()
        conn.connect()
        return conn, None

    def get_atr(self, handle):
        return bytes(handle.getATR())

    def transmit(self, handle, protocol, data):
        resp, sw1, sw2 = handle.transmit(list(data))
        return bytes(resp) + bytes([sw1, sw2])

    def reconnect(self, handle, disposition=SCARD_LEAVE_CARD):
        handle.reconnect(disposition=disposition)
        return None

    def disconnect(self, handle):
        handle.disconnect()

    def release(self):
        pass
