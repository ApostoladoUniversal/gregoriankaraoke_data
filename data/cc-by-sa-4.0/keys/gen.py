#!/usr/bin/env python3
# SPDX-License-Identifier: CC-BY-SA-4.0
"""Gera os WAVs de tons e teclas do teclado.

Saídas (nesta pasta, por padrão):
  _sil.wav              0,1 s de silêncio
  _tone12.wav ...        tons puros de 1,8 s (MIDI 12..32)
  key_12.wav ...         teclas de 2,0 s (MIDI 12..32, seno puro)
  key_33.wav ...         teclas de 2,1 s (MIDI 33..84, SF2 via FluidSynth)

Dependências:
  - Python 3 (wave, math, struct, subprocess, tempfile).
  - fluidsynth no PATH, somente para key_33..84.
  - SoundFont ChurchOrganEmulation-20190924.sf2 (CC0), por padrão em
    ../../../gitignore/ChurchOrganEmulation-20190924.sf2.

O SF2 não possui samples abaixo de MIDI 33; por isso key_12..32 são seno puro.
As teclas 33..84 são renderizadas com o SF2, com o mesmo programa 74 do órgão,
e recebem 0,1 s de silêncio no início e no fim.

Uso:
  python3 gen.py
  python3 gen.py --out /tmp/opencode/keys-test
  python3 gen.py --sf2 /caminho/ChurchOrganEmulation-20190924.sf2
"""

import argparse
import math
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path


SR = 22050
AMP = 0.708
LOW_AMP = 0.354
DUR = 1.8
SIL = 0.1
FADE = 0.004

# key_33..84: nota de 2,0 s no SF2, usada por 1,9 s, com 0,1 s de silêncio
# em cada ponta. O ganho é o ganho padrão do FluidSynth (0,2) menos 4 dB.
KEY_BODY = 1.9
KEY_GAIN = 0.2 * 10 ** (-4 / 20)

HERE = Path(__file__).resolve().parent
DEFAULT_SF2 = HERE.parents[2] / "gitignore" / "ChurchOrganEmulation-20190924.sf2"


def pcm_silence(seconds=SIL):
    return b"\0" * (int(SR * seconds) * 4)


def pcm_tone(midi, amp=AMP, dur=DUR):
    n = int(SR * dur)
    hz = 440.0 * 2 ** ((midi - 69) / 12.0)
    pcm = bytearray()
    for i in range(n):
        t = i / SR
        env = min(1.0, t / FADE, (dur - t) / FADE)
        v = int(32767 * amp * env * math.sin(2 * math.pi * hz * t))
        pcm += struct.pack("<hh", v, v)
    return bytes(pcm)


def write_wav(out, name, pcm):
    with wave.open(str(out / name), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


def vlq(value):
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def write_note_midi(path, midi):
    """MIDI mínimo: programa 74, nota por 2 s a 60 BPM, velocity 127."""
    division = 480
    ticks = division * 2  # 2 tempos a 60 BPM = 2 s
    track = bytearray()
    track += b"\x00\xff\x51\x03" + (1_000_000).to_bytes(3, "big")
    track += b"\x00\xc0\x4a"  # program change 74
    track += b"\x00\x90" + bytes((midi, 127))
    track += vlq(ticks) + b"\x80" + bytes((midi, 0))
    track += b"\x00\xff\x2f\x00"

    header = (
        b"MThd"
        + (6).to_bytes(4, "big")
        + (0).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + division.to_bytes(2, "big")
    )
    path.write_bytes(header + b"MTrk" + len(track).to_bytes(4, "big") + track)


def read_high_key(tmpdir, midi, sf2):
    mid = tmpdir / f"key_{midi}.mid"
    wav = tmpdir / f"key_{midi}.wav"
    write_note_midi(mid, midi)

    subprocess.run(
        [
            "fluidsynth",
            "-F", str(wav),
            "-T", "wav",
            "-r", str(SR),
            "-g", str(KEY_GAIN),
            "-i",
            str(sf2),
            str(mid),
        ],
        check=True,
        capture_output=True,
    )

    with wave.open(str(wav), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (2, 2, SR):
            raise RuntimeError(
                f"Formato inesperado do FluidSynth: "
                f"{w.getnchannels()} canais, {w.getsampwidth() * 8} bits, "
                f"{w.getframerate()} Hz"
            )
        raw = w.readframes(w.getnframes())

    need = int(SR * KEY_BODY) * 4
    if len(raw) < need:
        raise RuntimeError(
            f"FluidSynth gerou {len(raw) // 4} frames para MIDI {midi}; "
            f"esperado pelo menos {need // 4}"
        )

    return pcm_silence() + raw[:need] + pcm_silence()


def main():
    parser = argparse.ArgumentParser(description="Gera tons e teclas de órgão.")
    parser.add_argument("--out", default=str(HERE), help="pasta de saída")
    parser.add_argument("--sf2", default=str(DEFAULT_SF2), help="SoundFont .sf2")
    args = parser.parse_args()

    out = Path(args.out).expanduser().resolve()
    sf2 = Path(args.sf2).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    if not sf2.is_file():
        parser.error(f"SoundFont não encontrado: {sf2}")
    if shutil.which("fluidsynth") is None:
        parser.error("fluidsynth não encontrado no PATH")

    write_wav(out, "_sil.wav", pcm_silence())
    for midi in range(12, 33):
        write_wav(out, f"_tone{midi}.wav", pcm_tone(midi))

    for midi in range(12, 33):
        amp = AMP if midi == 32 else LOW_AMP
        write_wav(
            out,
            f"key_{midi}.wav",
            pcm_silence() + pcm_tone(midi, amp) + pcm_silence(),
        )

    print(f"[keys] graves e tons gerados em {out}")
    with tempfile.TemporaryDirectory(prefix="organ-keys-") as tmp:
        tmpdir = Path(tmp)
        for midi in range(33, 85):
            write_wav(out, f"key_{midi}.wav", read_high_key(tmpdir, midi, sf2))
            if (midi - 32) % 8 == 0:
                print(f"[keys] SF2: key_{midi}.wav")

    print(f"[keys] pronto: 73 teclas em {out}")


if __name__ == "__main__":
    main()
