# smartcard-personalizer

Drive smart-card personalization scripts over a real
[pcsc-lite](https://pcsclite.apdu.fr/) daemon. Binds the pcsc-lite client
library directly via `ctypes` (no pyscard, no pysim), so it reaches whichever
`pcscd` is actually running — including a locally-launched one that Apple's
bundled `PCSC.framework` would never see.

## Install

```
uv sync           # dev
uv tool install . # as a global CLI
```

Runs on Python ≥ 3.9. The only runtime dependency is `enlighten` (progress bar).

## Usage

```
smartcard-personalizer --list-readers
smartcard-personalizer -s card.pf                 # run one script
smartcard-personalizer -s a.pf -s b.pf            # run several, one card session
smartcard-personalizer --reader Omnikey -s a.pf   # pick reader by name substring or index
```

Also runnable as `pf-loader` or `python -m smartcard_personalizer`.

### Pointing at the pcsc-lite library

Defaults to `/opt/homebrew/opt/pcsc-lite/lib/libpcsclite_real.dylib` on macOS
and `libpcsclite.so.1` elsewhere. Override with `--pcsc-lib PATH` or
`PCSC_LIB`.

## `.pf` script format

One directive per line; scripts share a single card session and run in order.

| Line       | Meaning                                                      |
|------------|-------------------------------------------------------------|
| `> AA BB`  | send an APDU (hex bytes; long tokens like `AABB` are split) |
| `< 90 00`  | assert the last response; `XX` matches any byte             |
| `WARM`     | warm reset (reconnect, reset card)                          |
| `COLD`     | cold reset (reconnect, power-cycle card)                    |
| `: label`  | progress-bar stage label                                    |
| `# comment`| ignored; blank lines ignored                                |

A failed `<` assertion aborts with a non-zero exit code.

```
: select
> 00 A4 04 00 07 A0 00 00 00 03 00 00
< XX XX 90 00
```
