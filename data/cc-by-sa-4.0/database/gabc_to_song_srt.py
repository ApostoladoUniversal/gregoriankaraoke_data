#!/usr/bin/env python3
"""gabc_to_song_srt.py — GABC -> song.srt (syllable + phonetic subtitles).

Self-contained generator: all parser/DS/MIDI/subtitle/rescale code is in this
file. It does NOT import any other project file. Python standard library only.

Source GABC: data/cc0-1.0/gabc/<book>/<song-id>.gabc (copied from the GregoBase corpus)

Output: song.srt

Usage (from .../data/cc-by-sa-4.0/database):
  python gabc_to_song_srt.py --song pater-noster-a
  python gabc_to_song_srt.py ../../cc0-1.0/gabc/graduale_romanum/pater-noster-a.gabc -o graduale_romanum/pater-noster-a

Options:
  --song ID          find the GABC and the song folder automatically
  -o, --out DIR      output directory (default: current directory)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults / external tools. Only the organ scripts use fluidsynth/ffmpeg.
# ---------------------------------------------------------------------------
DEFAULT_SOUNDFONT = Path(os.environ.get(
    "GREGORIAN_SOUNDFONT",
    "../../../gitignore/ChurchOrganEmulation-20190924.sf2",
))
DEFAULT_FFMPEG = os.environ.get(
    "GREGORIAN_FFMPEG",
    "ffmpeg",
)
DEFAULT_FLUIDSYNTH = os.environ.get("GREGORIAN_FLUIDSYNTH", "fluidsynth")
SOUNDFONT = DEFAULT_SOUNDFONT
FFMPEG = DEFAULT_FFMPEG
FLUIDSYNTH = DEFAULT_FLUIDSYNTH

TEMPO_MID = 107
TEMPO_DS = 160
WORD_SILENCE = 0.08
ORGAN_PITCH = -12

# Paths relative to this file: .../data/cc-by-sa-4.0/database/<script>.py
DB_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(__file__).resolve().parents[2]
GABC_DIR = DATA_ROOT / "cc0-1.0" / "gabc"
MANIFEST = DB_ROOT / "songs.json"

# ---------------------------------------------------------------------------
# GABC parser (inlined; no project imports).
# ---------------------------------------------------------------------------
DUREE_EPISEME = 1.7

_SP_MAP = {
    "'ae": "æ",       # ǽ → æ (G2P já trata æ → /e/)
    "'oe": "œ",       # œ́ → œ (G2P já trata œ → /e/)
    "R/": "\u211f",   # ℟
    "V/": "\u2123",   # ℣
    "A/": "A\u20e6",  # A⃦ — A barrado
    "~": "~",
}


def _resolve_sp(text):
    """Resolve <sp>...</sp> special chars and strip other HTML tags."""
    text = re.sub(r'<sp>(.*?)</sp>',
                  lambda m: _SP_MAP.get(m.group(1), m.group(1)), text)
    text = re.sub(r'<[^>]+>', '', text)
    return text
DUREE_AVANT_QUILISMA = 2.0
DUREE_POINT = 2.3

GABC_LETTERS = set("abcdefghijklm")
GAMME = (57, 59, 60, 62, 64, 65, 67)
GABC_ORDER = "abcdefghijklm"

CLEF_DECALAGE = {
    "c4": 0, "c3": 2, "c2": 4, "c1": 6,
    "f4": 3, "f3": 5, "f2": 0, "f1": 2,
}
CLEF_BEMOL = {
    "c4": "bi", "c3": "g", "c2": "el", "c1": "cj",
    "f4": "fm", "f3": "dk", "f2": "bi", "f1": "g",
}

DUR_BAR = {"`": 0, ",": 0, ";": 0.5, ":": 1, "::": 2.2}


class GabcNote:
    __slots__ = ('letter', 'pitch', 'duration', 'shape', 'lyric',
                 'is_silence', 'is_word_start')
    def __init__(self, letter='', pitch=60, duration=1.0, shape='',
                 lyric='', is_silence=False, is_word_start=False):
        self.letter = letter
        self.pitch = pitch
        self.duration = duration
        self.shape = shape
        self.lyric = lyric
        self.is_silence = is_silence
        self.is_word_start = is_word_start


class GabcParser:
    def __init__(self):
        self.clef = None
        self.pitch_map = None
        self.accidentals = {}
        self.key_flat_letters = ''
        self._last_lyric = ''

    def parse_file(self, path):
        with open(path, encoding='utf-8') as f:
            return self.parse(f.read())

    def parse(self, content):
        body = self._extract_body(content)
        notes = []
        lyric_buf = []
        mode = 0
        i = 0
        L = len(body)

        while i < L:
            ch = body[i]

            if mode == 0:
                if ch == '(':
                    mode = 1
                elif ch in '{}':
                    pass
                elif ch == ' ':
                    self.accidentals = {}
                    if lyric_buf and lyric_buf[-1] != ' ':
                        lyric_buf.append(' ')
                    elif not lyric_buf:
                        lyric_buf.append(' ')
                elif ch in ',;:`':
                    pass
                elif ch != '-':
                    lyric_buf.append(ch)

            elif mode == 1:
                if ch == ')':
                    mode = 0

                elif ch == '[':
                    mode = 2

                elif ch in 'cf':
                    j = i + 1
                    clef_str = ch
                    if j < L and body[j] == 'b':
                        clef_str += 'b'
                        j += 1
                    if j < L and body[j].isdigit():
                        clef_str += body[j]
                        j += 1
                    clean = clef_str.replace('b', '')
                    if len(clef_str) >= 2 and clean in CLEF_DECALAGE:
                        self._set_clef(clef_str)
                        i = j - 1
                    else:
                        n = self._make_note(ch, lyric_buf)
                        if n:
                            notes.append(n)

                elif ch.lower() in GABC_LETTERS:
                    n = self._make_note(ch, lyric_buf)
                    if n:
                        notes.append(n)

                elif ch == 'w':
                    if len(notes) >= 2:
                        notes[-2].duration = max(notes[-2].duration, DUREE_AVANT_QUILISMA)

                elif ch in 'oOsS':
                    if notes:
                        n = GabcNote(letter=notes[-1].letter,
                                     pitch=notes[-1].pitch, shape=ch)
                        notes.append(n)

                elif ch in 'vV':
                    if notes:
                        notes[-1].shape = 'virga'

                elif ch in ',;:`':
                    if ch == ':':
                        nch = body[i+1] if i+1 < L else ''
                        if nch == ':':
                            bar_key = '::'
                            i += 1
                        else:
                            bar_key = ':'
                    else:
                        bar_key = ch
                    self._apply_bar(notes, bar_key)

                elif ch == '_' and notes:
                    notes[-1].duration = max(notes[-1].duration, DUREE_EPISEME)
                    notes[-1].shape = 'episeme'

                elif ch == '.' and notes:
                    notes[-1].duration = max(notes[-1].duration, DUREE_POINT)
                    notes[-1].shape = 'punctum'

                elif ch == 'x':
                    if notes:
                        last = notes[-1]
                        self.accidentals[last.letter] = -1
                        if last.letter not in self.key_flat_letters:
                            last.pitch -= 1

                elif ch == 'y':
                    if notes:
                        last = notes[-1]
                        self.accidentals[last.letter] = 0
                        if last.letter in self.key_flat_letters:
                            last.pitch += 1

                elif ch == '+':
                    if notes:
                        notes[-1].shape = 'custos'

                elif ch == '#':
                    if notes:
                        last = notes[-1]
                        self.accidentals[last.letter] = 1
                        if last.letter in self.key_flat_letters:
                            last.pitch += 2
                        else:
                            last.pitch += 1

                elif ch in '/ ':
                    if len(notes) >= 2 and notes[-1].duration < notes[-2].duration:
                        notes[-1].duration = notes[-2].duration

            elif mode == 2:
                if ch == ']':
                    mode = 1

            i += 1

        return notes

    def _extract_headers(self, content):
        hdr = content.split('%%', 1)[0] if '%%' in content else ''
        result = {}
        for line in hdr.split('\n'):
            if ':' not in line:
                continue
            parts = line.split(':', 1)
            key = parts[0].strip()
            val = parts[1].replace(';', '').replace('\r', '').strip()
            result[key] = val
        return result

    def _extract_body(self, content):
        body = content.split('%%', 1)[1] if '%%' in content else content
        body = re.sub(r'%.*\n?', '', body)
        body = re.sub(r'\n', ' ', body)
        body = re.sub(r'\[[^\[\]]*\]', '', body)
        body = _resolve_sp(body)
        return body

    def _build_pitch_map(self, clef_key):
        dec = CLEF_DECALAGE.get(clef_key, 0)
        oct_off = -12 if clef_key == "f3" else 0
        idx = dec - 1
        pm = {}
        for letter in GABC_ORDER:
            idx += 1
            if idx == 7:
                idx = 0
                oct_off += 12
            pm[letter] = GAMME[idx] + oct_off
        return pm

    def _set_clef(self, raw):
        ct = raw[0]
        has_flat = 'b' in raw
        line_part = re.sub(r'[cfb]', '', raw)
        line = int(line_part) if line_part else 4
        key = f"{ct}{line}"
        self.clef = key
        self.pitch_map = self._build_pitch_map(key)
        self.key_flat_letters = CLEF_BEMOL.get(key, '') if has_flat else ''
        self.accidentals = {}

    def _make_note(self, ch, lyric_buf):
        letter = ch.lower()
        if letter not in GABC_LETTERS:
            return None
        if self.clef is None:
            self._set_clef('c4')
        pitch = self.pitch_map.get(letter, 60)
        adj = self.accidentals.get(letter, None)
        if adj == -1:
            pitch -= 1
        elif adj == 1:
            pitch += 1
        elif adj is None and letter in self.key_flat_letters:
            pitch -= 1
        n = GabcNote(letter=letter, pitch=pitch, duration=1.0)
        new_lyric = None
        if lyric_buf:
            full = ''.join(lyric_buf).strip()
            if full:
                new_lyric = full.split()[-1] if ' ' in full else full
                n.is_word_start = bool(lyric_buf[0] == ' ')
            lyric_buf.clear()
        if new_lyric:
            n.lyric = new_lyric
            self._last_lyric = new_lyric
        elif self._last_lyric:
            n.lyric = self._last_lyric
        return n

    def _apply_bar(self, notes, bar_key):
        self.accidentals = {}
        poser = DUR_BAR.get(bar_key, 0)
        if poser > 0 and notes:
            notes[-1].duration += poser


def _auto_transposition(notes):
    if not notes:
        return 0
    pitches = [n.pitch for n in notes]
    return 66 - int((min(pitches) + max(pitches)) / 2)


# Euouae vowel sequence: sEculOrUm Amen → E u o u a e
_EUOUAE_VOWELS = ['e', 'u', 'o', 'u', 'a', 'e']


def _strip_euouae(notes):
    """Remove euouae cadence from end of note list.

    Matches by lyric syllable sequence (E u o u a e), not note letters,
    since euouae is the vowel pattern from 'sEculOrUm Amen'.
    """
    if len(notes) < 6:
        return False
    rev = []
    last = None
    for n in reversed(notes):
        l = (n.lyric or '').rstrip('.').lower()
        if not l:
            break
        if l != last:
            rev.append(l)
            last = l
    if len(rev) >= 6 and rev[:6] == list(reversed(_EUOUAE_VOWELS)):
        count = 0
        dc = 0
        last = None
        for n in reversed(notes):
            l = (n.lyric or '').rstrip('.').lower()
            if l != last:
                dc += 1
                last = l
            count += 1
            if dc >= 6:
                break
        del notes[-count:]
        return True
    return False


def _sanitize_latin1(text):
    if isinstance(text, str):
        return text.encode('latin-1', errors='replace').decode('latin-1')
    return text

def _parse_metadata(content):
    hdrs = GabcParser()._extract_headers(content)
    keys = ['office-part', 'mode', 'book', 'commentary']
    meta = {k: hdrs.get(k, '') for k in keys}
    meta['name'] = hdrs.get('name', '')
    return meta


# ---------------------------------------------------------------------------
# GABC -> DS (inlined DiffSinger pre-voice generator; no project imports).
# ---------------------------------------------------------------------------
_ACCENT_MAP = str.maketrans({
    'á': 'a', 'à': 'a', 'â': 'a', 'ã': 'a', 'ä': 'a',
    'é': 'e', 'è': 'e', 'ê': 'e', 'ë': 'e',
    'í': 'i', 'ì': 'i', 'î': 'i', 'ï': 'i',
    'ó': 'o', 'ò': 'o', 'ô': 'o', 'õ': 'o', 'ö': 'o',
    'ú': 'u', 'ù': 'u', 'û': 'u', 'ü': 'u',
    'ý': 'y', 'ÿ': 'y',
    'æ': 'e', 'Æ': 'e',
    'œ': 'e', 'Œ': 'e',
    'ñ': 'n', 'Ñ': 'n',
})

_G2P = {
    'a': 'a', 'b': 'b', 'd': 'd', 'e': 'e', 'f': 'f', 'g': 'g',
    'i': 'i', 'k': 'k', 'l': 'l', 'm': 'm', 'n': 'n', 'o': 'o',
    'p': 'p', 'r': 'r', 's': 's', 't': 't', 'u': 'u', 'v': 'v',
    'z': 'z', 'w': 'w', 'j': 'j',
    'c': 'k', 'y': 'i', 'q': 'k',
}


_VOWEL_PHONEMES = {'a', 'ae', 'an', 'ax', 'e', 'eh', 'en', 'i', 'i0',
                   'in', 'o', 'oh', 'on', 'u', 'u0', 'un'}


def _check_context(s, i, char, lookahead):
    if i + 1 < len(s) and s[i] == char and s[i + 1] in lookahead:
        return True
    if i + 1 < len(s) and s[i] == char:
        nxt = s[i + 1:i + 3] if i + 2 < len(s) else s[i + 1:]
        if nxt in lookahead or nxt in ('ae', 'oe'):
            return True
    return False


def latin_to_phonemes(text, lang='pt', vowel_only=False, coda_only=False):
    result = []
    s = text.lower().translate(_ACCENT_MAP)
    i = 0
    while i < len(s):
        if not s[i].isalpha():
            i += 1
            continue
        if s[i] == 'h':
            i += 1
            continue
        if i + 1 < len(s) and s[i:i+2] in ('ae', 'oe'):
            result.append(f'{lang}/e'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'ph':
            result.append(f'{lang}/f'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'th':
            result.append(f'{lang}/t'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'rh':
            result.append(f'{lang}/r'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'gn':
            result.append(f'{lang}/nh'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'nh':
            result.append(f'{lang}/nh'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'lh':
            result.append(f'{lang}/lh'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'ch':
            result.append(f'{lang}/k'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'rr':
            result.append(f'{lang}/rr'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'ss':
            result.append(f'{lang}/s'); i += 2
        elif i + 2 < len(s) and s[i:i+2] == 'sc' and s[i+2] in 'eiy':
            result.append(f'{lang}/sh'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'gu' and i + 2 < len(s) and s[i+2] in 'aeiou':
            result.append(f'{lang}/g'); result.append(f'{lang}/w'); i += 2
        elif i + 1 < len(s) and s[i:i+2] == 'qu':
            result.append(f'{lang}/k'); result.append(f'{lang}/w'); i += 2
        elif _check_context(s, i, 'c', set('eiy')):
            result.append(f'{lang}/ch'); i += 1
        elif _check_context(s, i, 'g', set('eiy')):
            result.append(f'{lang}/dj'); i += 1
        elif s[i] == 't' and i + 2 < len(s) and s[i+1] == 'i' and s[i+2] in 'aeou' and (i == 0 or s[i-1] not in 'stx'):
            result.append(f'{lang}/t'); result.append(f'{lang}/s'); result.append(f'{lang}/i'); i += 2
        elif s[i] == 'x':
            result.append(f'{lang}/k')
            result.append(f'{lang}/s')
            i += 1
        else:
            ph = _G2P.get(s[i], s[i])
            result.append(f'{lang}/{ph}')
            i += 1
    if coda_only:
        last_v = -1
        for j, ph in enumerate(result):
            if ph.split('/')[-1] in _VOWEL_PHONEMES:
                last_v = j
        result = result[:last_v + 1] if last_v >= 0 else result[-1:]
    if vowel_only:
        vowel_phs = [ph for ph in result if ph.split('/')[-1] in _VOWEL_PHONEMES]
        result = vowel_phs[-1:] if vowel_phs else result[-1:]
    if not result:
        result.append('SP')
    return result

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

MIN_DUR = {
    'plosive': 0.06, 'fricative': 0.08, 'nasal': 0.08,
    'liquid': 0.08, 'glide': 0.06, 'vowel': 0.10,
    'sp': 0.05, 'ap': 0.20, 'other': 0.08,
}


def _distribute_ph_durations(phs, total_dur):
    n = len(phs)
    if n == 0:
        return []
    if n == 1:
        return [total_dur]

    def ph_type(ph):
        b = ph.split('/')[-1] if '/' in ph else ph
        plos = {'p', 'b', 't', 'd', 'k', 'g'}
        fric = {'f', 'v', 's', 'z', 'sh', 'ch', 'dj', 'j', 'x'}
        nas = {'m', 'n', 'nh', 'ng'}
        liq = {'l', 'lh', 'r', 'rr', 'rh', 'rw'}
        gli = {'w', 'y'}
        if b == 'SP':
            return 'sp'
        if b == 'AP':
            return 'ap'
        if b in _VOWEL_PHONEMES:
            return 'vowel'
        if b in plos:
            return 'plosive'
        if b in fric:
            return 'fricative'
        if b in nas:
            return 'nasal'
        if b in liq:
            return 'liquid'
        if b in gli:
            return 'glide'
        return 'other'

    mins = [MIN_DUR.get(ph_type(p), 0.08) for p in phs]
    tmin = sum(mins)
    if tmin >= total_dur:
        s = total_dur / tmin
        return [m * s for m in mins]
    rem = total_dur - tmin
    vowel_idx = [i for i, p in enumerate(phs) if ph_type(p) == 'vowel']
    if vowel_idx:
        epv = rem / len(vowel_idx)
        d = mins[:]
        for i in vowel_idx:
            d[i] += epv
        return d
    epp = rem / n
    return [m + epp for m in mins]


def _shorten_consonants(ph_seq, ph_dur, amount=0.04):
    LIQUID = {'pt/l', 'pt/lh', 'pt/r', 'pt/rr', 'pt/rh', 'pt/rw'}
    for i in range(len(ph_seq)):
        ph = ph_seq[i]
        if ph in LIQUID and i + 1 < len(ph_seq):
            old = float(ph_dur[i])
            new_val = max(old - amount, 0.02)
            diff = old - new_val
            ph_dur[i] = f"{new_val:.6f}"
            ph_dur[i+1] = f"{float(ph_dur[i+1]) + diff:.6f}"


def _apply_portamento(f0_seq, note_dur, ft, t_port=0.05):
    if t_port <= 0 or len(f0_seq) < 3:
        return
    vals = [float(v) for v in f0_seq]
    n_port = max(2, int(t_port / ft))
    frame_idx = 0
    for i, dur in enumerate(note_dur):
        nf = max(1, int(dur / ft))
        frame_idx += nf
        if i + 1 >= len(note_dur) or frame_idx >= len(vals):
            break
        f0_prev = vals[min(frame_idx - 1, len(vals) - 1)]
        f0_next = vals[min(frame_idx, len(vals) - 1)]
        if abs(f0_prev - f0_next) < 0.5:
            continue
        half = n_port // 2
        start = max(0, frame_idx - half)
        end = min(len(vals), frame_idx + half)
        for j in range(start, end):
            x = 8.0 * (j - frame_idx) / n_port
            w = 1.0 / (1.0 + math.exp(-x))
            vals[j] = f0_prev + (f0_next - f0_prev) * w
    for i in range(len(f0_seq)):
        f0_seq[i] = f"{vals[i]:.2f}"


def _apply_all_vibrato(f0_seq, note_dur, ft, hz=2.75, cents=20):
    if hz <= 0 or cents <= 0:
        return
    vals = [float(v) for v in f0_seq]
    frame_idx = 0
    for dur in note_dur:
        nf = max(1, int(dur / ft))
        start = frame_idx
        end = min(frame_idx + nf, len(vals))
        if nf < 6:
            frame_idx += nf
            continue
        note_cents = cents * min(1.0, dur / 0.5)
        note_hz = hz + random.uniform(-0.15, 0.15)
        ramp_len = max(2, int(nf * 0.3))
        for j in range(start, end):
            t = (j - start) * ft
            pos = (j - start) / nf
            env = 1.0
            if pos < 0.3:
                env = pos / 0.3
            elif pos > 0.7:
                env = (1.0 - pos) / 0.3
            semitones = note_cents / 100.0
            mod = 2.0 ** (semitones * env * math.sin(2 * math.pi * note_hz * t) / 12.0)
            vals[j] = vals[j] * mod
        frame_idx += nf
    for i in range(len(f0_seq)):
        f0_seq[i] = f"{vals[i]:.2f}"


def _apply_vibrato(f0_seq, note_dur, ft, hz=6.0, cents=40):
    if hz <= 0 or cents <= 0:
        return
    vals = [float(v) for v in f0_seq]
    frame_idx = 0
    last_start = 0
    for dur in note_dur:
        nf = max(1, int(dur / ft))
        last_start = frame_idx
        frame_idx += nf
    if not note_dur or note_dur[-1] < 0.8:
        return
    last_nf = max(1, int(note_dur[-1] / ft))
    total_frames = min(last_start + last_nf, len(vals))
    vibrato_start = last_start + int(last_nf * 0.6)
    for j in range(vibrato_start, total_frames):
        t = (j - vibrato_start) * ft
        semitones = cents / 100.0
        mod = 2.0 ** (semitones * math.sin(2 * math.pi * hz * t) / 12.0)
        vals[j] = vals[j] * mod
    for i in range(len(f0_seq)):
        f0_seq[i] = f"{vals[i]:.2f}"


def _apply_scoop(f0_seq, note_hauteur, note_dur, ft, t_scoop=0.04, cents=15):
    if t_scoop <= 0:
        return
    vals = [float(v) for v in f0_seq]
    n_scoop = max(2, int(t_scoop / ft))
    half = n_scoop // 2
    frame_idx = 0
    for i, dur in enumerate(note_dur):
        nf = max(1, int(dur / ft))
        frame_idx += nf
        if i + 1 >= len(note_hauteur) or frame_idx >= len(vals):
            break
        interval = note_hauteur[i+1] - note_hauteur[i]
        if interval < 3:
            continue
        start = max(0, frame_idx - half)
        end = min(len(vals), frame_idx + half)
        for j in range(start, end):
            dist = 1.0 - abs(j - frame_idx) / half
            bump = 2.0 ** (cents * max(dist, 0.0) / 1200.0)
            vals[j] *= bump
    for i in range(len(f0_seq)):
        f0_seq[i] = f"{vals[i]:.2f}"


def _apply_messa_di_voce(f0_seq, note_dur, ft, cents=5.0):
    if cents <= 0:
        return
    vals = [float(v) for v in f0_seq]
    frame_idx = 0
    for dur in note_dur:
        nf = max(1, int(dur / ft))
        if dur >= 1.5:
            start = frame_idx
            end = min(frame_idx + nf, len(vals))
            mid_f = (start + end) / 2.0
            for j in range(start, end):
                t = (j - mid_f) / (nf / 2.0)
                parabola = 1.0 - (t * t)
                bump = 2.0 ** (cents * max(parabola, 0.0) / 1200.0)
                vals[j] *= bump
        frame_idx += nf
    for i in range(len(f0_seq)):
        f0_seq[i] = f"{vals[i]:.2f}"


def _apply_pitch_drift(f0_seq, ft, cents=3.0, period=15.0):
    if cents <= 0 or period <= 0:
        return
    vals = [float(v) for v in f0_seq]
    for j in range(len(vals)):
        t = j * ft
        mod = 2.0 ** (cents * math.sin(2 * math.pi * t / period) / 1200.0)
        vals[j] *= mod
    for i in range(len(f0_seq)):
        f0_seq[i] = f"{vals[i]:.2f}"


def _apply_ictus_pulse(f0_seq, note_dur, ft, interval=3, cents=5.0):
    if cents <= 0 or interval <= 0:
        return
    vals = [float(v) for v in f0_seq]
    frame_idx = 0
    n_pulse = max(2, int(0.12 / ft))
    for i, dur in enumerate(note_dur):
        nf = max(1, int(dur / ft))
        if (i + 1) % interval == 0:
            p_frames = min(nf, n_pulse)
            start = frame_idx
            end = min(frame_idx + p_frames, len(vals))
            for j in range(start, end):
                t = (j - start) / p_frames
                envelope = 1.0 - t
                bump = 2.0 ** (cents * envelope / 1200.0)
                vals[j] *= bump
        frame_idx += nf
    for i in range(len(f0_seq)):
        f0_seq[i] = f"{vals[i]:.2f}"


def _apply_pitch_jitter(f0_seq, ft, cents=2.0, hz=12.0):
    if cents <= 0 or hz <= 0:
        return
    vals = [float(v) for v in f0_seq]
    for j in range(len(vals)):
        t = j * ft
        jit = cents * (math.sin(2*math.pi*hz*t) + math.sin(2*math.pi*hz*1.414*t)) / 2
        mod = 2.0 ** (jit / 1200.0)
        vals[j] *= mod
    for i in range(len(f0_seq)):
        f0_seq[i] = f"{vals[i]:.2f}"


def _apply_descending_scoop(f0_seq, note_hauteur, note_dur, ft, t_scoop=0.04, cents=10):
    if t_scoop <= 0:
        return
    vals = [float(v) for v in f0_seq]
    n_scoop = max(2, int(t_scoop / ft))
    half = n_scoop // 2
    frame_idx = 0
    for i, dur in enumerate(note_dur):
        nf = max(1, int(dur / ft))
        frame_idx += nf
        if i + 1 >= len(note_hauteur) or frame_idx >= len(vals):
            break
        interval = note_hauteur[i+1] - note_hauteur[i]
        if interval > -3:
            continue
        start = max(0, frame_idx - half)
        end = min(len(vals), frame_idx + half)
        for j in range(start, end):
            dist = 1.0 - abs(j - frame_idx) / half
            dip = 2.0 ** (-cents * max(dist, 0.0) / 1200.0)
            vals[j] *= dip
    for i in range(len(f0_seq)):
        f0_seq[i] = f"{vals[i]:.2f}"


def _apply_rubato(dur_assign, amount=0.3):
    if amount <= 0 or len(dur_assign) < 5:
        return
    n = len(dur_assign)
    n_aff = max(2, min(8, n // 4))
    total_stretch = 0.0
    for i in range(n_aff):
        factor = 1.0 + amount * ((n_aff - i) / n_aff)
        old = dur_assign[-(n_aff - i)]
        new = old * factor
        total_stretch += new - old
        dur_assign[-(n_aff - i)] = new
    if total_stretch > 0 and n > n_aff:
        sum_prev = sum(dur_assign[:n - n_aff])
        if sum_prev > 0:
            shrink = 1.0 - total_stretch / sum_prev
            if shrink >= 0.8:
                for i in range(n - n_aff):
                    dur_assign[i] *= shrink


def midi_note_name(pitch):
    return f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}"


def _extract_midi_lyrics(mid_path):
    with open(mid_path, 'rb') as f:
        data = f.read()
    ntrks = struct.unpack('>H', data[10:12])[0]
    pos = 14
    track_name = ''
    syl_texts = []
    word_starts = []
    for _ in range(ntrks):
        if data[pos:pos+4] != b'MTrk':
            break
        trk_len = struct.unpack('>I', data[pos+4:pos+8])[0]
        pos += 8
        end = pos + trk_len
        while pos < end:
            while True:
                b = data[pos]; pos += 1
                if not (b & 0x80):
                    break
            event = data[pos]; pos += 1
            if 0x80 <= event < 0xF0:
                if (event & 0xF0) in (0xC0, 0xD0):
                    pos += 1
                else:
                    pos += 2
            elif event == 0xFF:
                mtype = data[pos]; pos += 1
                mlen = 0
                while True:
                    b = data[pos]; pos += 1
                    mlen = (mlen << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                mdata = data[pos:pos+mlen]; pos += mlen
                t_raw = mdata.decode("latin-1", errors="replace")
                t = t_raw.strip()
                if mtype == 0x03:
                    track_name = t.split('|')[0].strip()
                elif mtype == 0x01 and t and not t.startswith('book:'):
                    if not any(kw in t.lower() for kw in ['commentary', 'office-part', 'mode']):
                        word_starts.append(t_raw.startswith(' '))
                        syl_texts.append(t.rstrip('-*'))
            elif event in (0xF0, 0xF7):
                while True:
                    b = data[pos]; pos += 1
                    if not (b & 0x80):
                        break
        pos = end
    text = ' '.join(syl_texts)
    text = re.sub(r'[.,;:*<>]+', '', text)
    text = text.replace('<sp>', '').replace('</sp>', '')
    text = ' '.join(text.split())
    if not text:
        text = track_name
    if word_starts:
        word_starts[0] = True
    return text, word_starts


def _read_midi_ticks_per_qn(mid_path):
    with open(mid_path, 'rb') as f:
        data = f.read()
    if data[0:4] != b'MThd':
        return 480, data
    ntrks = struct.unpack('>H', data[10:12])[0]
    div = struct.unpack('>H', data[12:14])[0]
    tpqn = div if not (div & 0x8000) else 480
    return tpqn, data, ntrks


def _read_midi_notes(mid_path):
    tpqn, data, ntrks = _read_midi_ticks_per_qn(mid_path)
    pos = 14
    tempo = 500000
    for _ in range(ntrks):
        if data[pos:pos+4] != b'MTrk':
            break
        trk_len = struct.unpack('>I', data[pos+4:pos+8])[0]
        pos += 8
        end = pos + trk_len
        tick = 0
        while pos < end:
            delta = 0
            while True:
                b = data[pos]; pos += 1
                delta = (delta << 7) | (b & 0x7F)
                if not (b & 0x80):
                    break
            tick += delta
            event = data[pos]; pos += 1
            if event >= 0x80 and event < 0xF0:
                if (event & 0xF0) in (0xC0, 0xD0):
                    pos += 1
                else:
                    pos += 2
            elif event == 0xFF:
                mtype = data[pos]; pos += 1
                mlen = 0
                while True:
                    b = data[pos]; pos += 1
                    mlen = (mlen << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                mdata = data[pos:pos+mlen]; pos += mlen
                if mtype == 0x51:
                    tempo = struct.unpack('>I', b'\x00' + mdata)[0]
            elif event in (0xF0, 0xF7):
                mlen = 0
                while True:
                    b = data[pos]; pos += 1
                    mlen = (mlen << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                pos += mlen
        pos = end
    sec_per_tick = tempo / 1_000_000 / tpqn

    pos = 14
    notes_raw = []
    tempo_events = []
    for _ in range(ntrks):
        if data[pos:pos+4] != b'MTrk':
            break
        trk_len = struct.unpack('>I', data[pos+4:pos+8])[0]
        pos += 8
        end = pos + trk_len
        tick = 0
        running_status = 0
        note_on = {}
        while pos < end:
            delta = 0
            while True:
                b = data[pos]; pos += 1
                delta = (delta << 7) | (b & 0x7F)
                if not (b & 0x80):
                    break
            tick += delta
            b0 = data[pos]; pos += 1
            if 0x80 <= b0 < 0xF0:
                running_status = b0
                status_hi = b0 & 0xF0
                if status_hi in (0xC0, 0xD0):
                    b2 = data[pos]; pos += 1
                    b3 = 0
                else:
                    b2 = data[pos]; pos += 1
                    b3 = data[pos]; pos += 1
                    if status_hi == 0x80:
                        b3 = 0
            elif running_status and b0 < 0x80:
                b2 = b0
                status_hi = running_status & 0xF0
                if status_hi in (0xC0, 0xD0):
                    b3 = 0
                else:
                    b3 = data[pos]; pos += 1
            else:
                if b0 == 0xFF:
                    mtype = data[pos]; pos += 1
                    mlen = 0
                    while True:
                        b = data[pos]; pos += 1
                        mlen = (mlen << 7) | (b & 0x7F)
                        if not (b & 0x80):
                            break
                    mdata = data[pos:pos+mlen]; pos += mlen
                    if mtype == 0x51:
                        tempo_events.append((tick, struct.unpack('>I', b'\x00' + mdata)[0]))
                elif b0 in (0xF0, 0xF7):
                    mlen = 0
                    while True:
                        b = data[pos]; pos += 1
                        mlen = (mlen << 7) | (b & 0x7F)
                        if not (b & 0x80):
                            break
                    pos += mlen
                continue

            key = b2
            is_on = (status_hi == 0x90 and b3 > 0)
            is_off = (status_hi == 0x80) or (status_hi == 0x90 and b3 == 0)
            if is_on:
                note_on[key] = tick
            elif is_off and key in note_on:
                start_tick = note_on.pop(key)
                dur_ticks = tick - start_tick
                notes_raw.append({
                    'pitch': b2,
                    'start_tick': start_tick,
                    'dur_ticks': dur_ticks,
                })
        pos = end

    notes_raw.sort(key=lambda x: x['start_tick'])
    tempo_events.sort(key=lambda x: x[0])
    def _tick_to_sec(tk):
        sec = 0.0
        prev_t = 0
        t = 500000
        for tt, tm in tempo_events:
            if tk <= tt:
                break
            sec += (tt - prev_t) * t / (tpqn * 1_000_000)
            prev_t, t = tt, tm
        sec += (tk - prev_t) * t / (tpqn * 1_000_000)
        return sec
    return [{
        'pitch': n['pitch'],
        'time': _tick_to_sec(n['start_tick']),
        'duration': max(_tick_to_sec(n['start_tick'] + n['dur_ticks']) - _tick_to_sec(n['start_tick']), 0.1),
    } for n in notes_raw]


def _text_to_syllables(words):
    if not words:
        return []
    vowels = 'aeiouyAEIOUYáéíóúàèìòùâêîôûãẽĩõũ'
    result = []
    for w in words:
        w = w.replace('-', ' ').replace('.', '').replace(',', '').strip()
        if not w:
            continue
        parts = w.split()
        if len(parts) > 1:
            result.extend(parts)
        elif len(w) <= 3 and any(c in vowels for c in w):
            result.append(w)
        else:
            syls = []
            cur = ''
            for i, ch in enumerate(w):
                cur += ch
                if i + 1 < len(w) and w[i] not in vowels and w[i+1] in vowels and len(cur) > 1:
                    syls.append(cur)
                    cur = ''
            if cur:
                syls.append(cur)
            if not syls:
                syls.append(w)
            result.extend(syls)
    return result


def _syllables_and_word_ends(words):
    if not words:
        return [], set()
    vowels = 'aeiouyAEIOUYáéíóúàèìòùâêîôûãẽĩõũ'
    result = []
    word_ends = set()
    for w in words:
        w = w.replace('-', ' ').replace('.', '').replace(',', '').strip()
        if not w:
            continue
        parts = w.split()
        if len(parts) > 1:
            result.extend(parts)
            word_ends.add(len(result) - 1)
        elif len(w) <= 3 and any(c in vowels for c in w):
            result.append(w)
            word_ends.add(len(result) - 1)
        else:
            syls = []
            cur = ''
            for i, ch in enumerate(w):
                cur += ch
                if i + 1 < len(w) and w[i] not in vowels and w[i+1] in vowels and len(cur) > 1:
                    syls.append(cur)
                    cur = ''
            if cur:
                syls.append(cur)
            if not syls:
                syls.append(w)
            result.extend(syls)
            word_ends.add(len(result) - 1)
    return result, word_ends


def mid_to_ds(mid_path, text, output_path=None, phoneme_lang='pt',
              pitches=None, durations=None, word_silence=0.08, word_starts=None,
              consonant_shortening=0.0, legato=False,
              portamento=0.0, vibrato_hz=0.0, vibrato_cents=40,
              scoop=0.0, messa_di_voce=0.0,
              pitch_drift=0.0, pitch_drift_period=15.0,
              ictus_interval=0, ictus_cents=5.0,
              pitch_jitter=0.0, pitch_jitter_hz=12.0,
              descending_scoop=0.0,
              rubato=0.0,
              f0_ondulation=False, f0_hz=1.375, f0_cents=20):
    mp = Path(mid_path)
    tempo = 160
    
    if pitches is not None and durations is not None:
        pitch_assign = list(pitches)
        dur_assign = list(durations)
        sec_per_beat = 60.0 / tempo
        dur_assign = [d * sec_per_beat for d in dur_assign]
    else:
        note_events = _read_midi_notes(mp)
        if not note_events:
            note_events = [
                {'pitch': p, 'time': i * 1.0, 'duration': 1.0}
                for i, p in enumerate([60, 62, 64, 65, 67, 69, 71, 72])
            ]
        pitch_assign = [n['pitch'] for n in note_events]
        dur_assign = [n['duration'] for n in note_events]

    ft = 0.005

    if rubato > 0 and len(dur_assign) > 4:
        _apply_rubato(dur_assign, rubato)

    note_seq = []
    note_dur = []
    note_hauteur = []
    ph_seq = []
    ph_dur = []
    ph_num = []
    is_sil = []
    syllables = []

    words = text.strip().split()
    word_end_indices = set()
    if words:
        if word_starts is not None and len(word_starts) == len(pitch_assign):
            syllables = words
            last_start = 0
            for i in range(1, len(word_starts)):
                if word_starts[i]:
                    word_end_indices.add(i - 1)
                    last_start = i
            word_end_indices.add(len(word_starts) - 1)
        elif len(words) != len(pitch_assign):
            syllables, word_end_indices = _syllables_and_word_ends(words)
        else:
            syllables = words
        for i, pitch in enumerate(pitch_assign):
            nd = dur_assign[i]
            note_seq.append(midi_note_name(pitch))
            note_dur.append(nd)
            note_hauteur.append(pitch)
            is_sil.append(False)

            if i < len(syllables):
                syl = syllables[i]
            else:
                syl = ''

            if phoneme_lang == 'pt' and syl:
                same_prev = (i > 0 and syl and syl == syllables[i-1])
                same_next = (i + 1 < len(syllables) and syl and syl == syllables[i+1])
                full = latin_to_phonemes(syl, lang='pt')
                if full and full[-1] == 'SP':
                    full = full[:-1]
                last_v = -1
                for j, ph in enumerate(full):
                    if ph.split('/')[-1] in _VOWEL_PHONEMES:
                        last_v = j
                if last_v >= 0:
                    onset_vowel = full[:last_v + 1]
                    vowel_coda = full[last_v:]
                    vowel_only = [full[last_v]]
                else:
                    onset_vowel = list(full)
                    vowel_coda = list(full)
                    vowel_only = full[-1:] if full else []

                if same_next and not same_prev:
                    phs = onset_vowel
                elif same_prev and not same_next:
                    phs = vowel_coda
                elif same_prev and same_next:
                    phs = vowel_only
                else:
                    phs = full
                if phs and phs[-1] == 'SP':
                    phs = phs[:-1]
                if not phs:
                    phs = ['SP']
                if (syl.rstrip('-').lower() == 'ti' and i + 1 < len(syllables)
                        and syllables[i+1] and syllables[i+1].lstrip('-')[0].lower().translate(_ACCENT_MAP) in 'aeou'):
                    pv = syllables[i-1].rstrip('-').lower() if i > 0 and syllables[i-1] else ''
                    if not (pv and pv[-1] in 'stx'):
                        phs = [f'{phoneme_lang}/t', f'{phoneme_lang}/s', f'{phoneme_lang}/i']
            else:
                phs = [c.lower() for c in syl] if syl else ['SP']

            durs = _distribute_ph_durations(phs, nd) if nd > 0 else [nd / len(phs)] * len(phs)

            if i in word_end_indices and word_silence > 0:
                if phs and phs[-1] == 'SP':
                    durs[-1] += word_silence
                else:
                    actual_dur = nd - word_silence
                    if actual_dur < nd * 0.3:
                        actual_dur = nd * 0.3
                    actual_silence = nd - actual_dur
                    durs = _distribute_ph_durations(phs, actual_dur)
                    phs.append('SP')
                    durs.append(actual_silence)

            ph_num.append(str(len(phs)))
            for p, d in zip(phs, durs):
                ph_seq.append(p)
                ph_dur.append(f"{d:.6f}")
    else:
        for i, pitch in enumerate(pitch_assign):
            nd = dur_assign[i]
            note_seq.append(midi_note_name(pitch))
            note_dur.append(nd)
            note_hauteur.append(pitch)
            is_sil.append(False)
            ph_num.append("1")
            ph_seq.append("SP")
            ph_dur.append(f"{nd:.6f}")

    f0_seq = []
    for h, d in zip(note_hauteur, note_dur):
        nf = max(1, int(d / ft))
        freq = 440.0 * (2.0 ** ((h - 69) / 12.0))
        f0_seq.extend([f"{freq:.2f}"] * nf)

    # ── Post-processing: humanização ──
    if consonant_shortening > 0 and ph_seq:
        _shorten_consonants(ph_seq, ph_dur, consonant_shortening)

    if portamento > 0:
        _apply_portamento(f0_seq, note_dur, ft, portamento)

    if scoop > 0:
        _apply_scoop(f0_seq, note_hauteur, note_dur, ft, scoop)

    if messa_di_voce > 0:
        _apply_messa_di_voce(f0_seq, note_dur, ft, messa_di_voce)

    if vibrato_hz > 0:
        _apply_vibrato(f0_seq, note_dur, ft, vibrato_hz, vibrato_cents)

    if pitch_drift > 0:
        _apply_pitch_drift(f0_seq, ft, pitch_drift, pitch_drift_period)

    if ictus_interval > 0:
        _apply_ictus_pulse(f0_seq, note_dur, ft, ictus_interval, ictus_cents)

    if pitch_jitter > 0:
        _apply_pitch_jitter(f0_seq, ft, pitch_jitter, pitch_jitter_hz)

    if descending_scoop > 0:
        _apply_descending_scoop(f0_seq, note_hauteur, note_dur, ft, descending_scoop)

    if f0_ondulation:
        _apply_all_vibrato(f0_seq, note_dur, ft, hz=f0_hz, cents=f0_cents)

    note_slur_values = ["0"] * len(note_seq)
    if legato:
        for i in range(1, min(len(syllables), len(note_seq))):
            if syllables[i] == syllables[i-1]:
                note_slur_values[i] = "1"

    ws_str = " ".join("1" if ws else "0" for ws in word_starts) if word_starts else ""
    segment = {
        "offset": 0,
        "text": text,
        "word_starts": ws_str,
        "note_seq": " ".join(note_seq),
        "note_dur": " ".join(f"{d:.6f}" for d in note_dur),
        "note_slur": " ".join(note_slur_values),
        "ph_num": " ".join(ph_num),
        "ph_seq": " ".join(ph_seq),
        "ph_dur": " ".join(ph_dur),
        "f0_seq": " ".join(f0_seq),
        "f0_timestep": str(ft),
        "seed": 42,
    }

    dp = Path(output_path)
    dp.parent.mkdir(parents=True, exist_ok=True)
    with open(dp, "w", encoding="utf-8") as f:
        json.dump([segment], f, ensure_ascii=False, indent=2)
    return dp


# ---------------------------------------------------------------------------
# DS -> SRT / LRC (inlined; no project imports).
# ---------------------------------------------------------------------------
def format_srt_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def gen_lrc(timings, title=None):
    lines = []
    if title:
        lines.append(f"[ti:{title}]")
    for sec, text in timings:
        cs = int(round(sec % 1, 2) * 100)
        lines.append(f"[{int(sec//60):02d}:{int(sec%60):02d}.{cs:02d}]{text}")
    return "\n".join(lines) + "\n"


COLOR = "#ff6600"


def srt_word_groups(syllables, word_starts):
    if not word_starts or len(word_starts) != len(syllables):
        return None
    groups = []
    cur = []
    for i, (syl, ws) in enumerate(zip(syllables, word_starts)):
        if ws and cur:
            groups.append(cur)
            cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    return groups


def srt_context_text(syllables, groups, i, ph_seq=None, ph_num=None):
    word_texts = [''.join(syllables[j] for j in g) for g in groups]
    # Deduped word texts (collapse consecutive duplicate syllables per word)
    deduped_word_texts = []
    deduped_groups = []
    for g in groups:
        dg = []
        for si in g:
            if not dg or syllables[si] != syllables[dg[-1]]:
                dg.append(si)
        deduped_groups.append(dg)
        deduped_word_texts.append(''.join(syllables[si] for si in dg))

    for wi, indices in enumerate(groups):
        if i not in indices:
            continue
        # ── line 1: textual (deduped display) ──
        dg = deduped_groups[wi]
        try:
            deduped_pos = dg.index(i)
        except ValueError:
            deduped_pos = next(idx for idx, si in enumerate(dg) if syllables[si] == syllables[i])
        parts = []
        for idx, si in enumerate(dg):
            if idx == deduped_pos:
                parts.append(f"<font color=\"{COLOR}\">{syllables[si]}</font>")
            else:
                parts.append(syllables[si])
        curr = ''.join(parts)
        prev_w = ' '.join(deduped_word_texts[max(0, wi - 2):wi]) if wi > 0 else None
        next_w = ' '.join(deduped_word_texts[wi + 1:min(len(groups), wi + 3)]) if wi < len(groups) - 1 else None

        if prev_w:
            line1 = f"{prev_w} {curr}{' ' + next_w if next_w else ''}"
        else:
            line1 = f"{curr}{' ' + next_w if next_w else ''}"

        # ── line 2: phonetic ──
        if ph_seq and ph_num:
            ph_idx2 = sum(ph_num[:indices[0]]) if indices else 0
            curr_ph_parts = []
            for si in indices:
                num_ph = ph_num[si] if si < len(ph_num) else 0
                phs = []
                for k in range(num_ph):
                    p = ph_seq[ph_idx2] if ph_idx2 < len(ph_seq) else ''
                    p_clean = p.split('/')[-1] if p not in ('SP', 'AP') else None
                    if p_clean:
                        phs.append(p_clean)
                    ph_idx2 += 1
                syl_ph = ''.join(phs)
                if si == i:
                    curr_ph_parts.append(f"<font color=\"{COLOR}\">{syl_ph}</font>")
                else:
                    curr_ph_parts.append(syl_ph)
            curr_ph = ''.join(curr_ph_parts)

            ph_idx2 = 0
            word_ph_texts = []
            for indices_g in groups:
                wph = []
                for si in indices_g:
                    num_ph = ph_num[si] if si < len(ph_num) else 0
                    for k in range(num_ph):
                        p = ph_seq[ph_idx2] if ph_idx2 < len(ph_seq) else ''
                        p_clean = p.split('/')[-1] if p not in ('SP', 'AP') else None
                        if p_clean:
                            wph.append(p_clean)
                        ph_idx2 += 1
                word_ph_texts.append(''.join(wph))

            prev_ph = ' '.join(word_ph_texts[max(0, wi - 2):wi]) if wi > 0 else None
            next_ph = ' '.join(word_ph_texts[wi + 1:min(len(groups), wi + 3)]) if wi < len(groups) - 1 else None

            if prev_ph:
                line2 = f"{prev_ph} {curr_ph}{' ' + next_ph if next_ph else ''}"
            else:
                line2 = f"{curr_ph}{' ' + next_ph if next_ph else ''}"
        else:
            line2 = line1

        return line1, line2
    return word_texts[0] if word_texts else '', ''


def ds_to_srt(ds_path, srt_path=None):
    with open(ds_path, encoding='utf-8') as f:
        segments = json.load(f)
    seg = segments[0]
    text = seg['text']
    syllables = text.split()
    note_durs = [float(x) for x in seg['note_dur'].split()]
    ws_raw = seg.get('word_starts', '')
    word_starts = [c == '1' for c in ws_raw.split()] if ws_raw else None
    ph_num = [int(x) for x in seg.get('ph_num', '').split()] if seg.get('ph_num') else None
    ph_seq = seg.get('ph_seq', '').split() if seg.get('ph_seq') else None

    n = min(len(syllables), len(note_durs))
    syllables = syllables[:n]
    note_durs = note_durs[:n]
    if word_starts and len(word_starts) > n:
        word_starts = word_starts[:n]
    if ph_num and len(ph_num) > n:
        ph_num = ph_num[:n]
        ph_seq = ph_seq[:sum(ph_num)]

    groups = srt_word_groups(syllables, word_starts)

    times = [0.0]
    for dur in note_durs:
        times.append(round(times[-1] + dur, 2))

    lines = []
    for i in range(len(syllables)):
        lines.append(f"{i + 1}")
        lines.append(f"{format_srt_time(times[i])} --> {format_srt_time(times[i + 1])}")
        if groups:
            l1, l2 = srt_context_text(syllables, groups, i, ph_seq, ph_num)
            lines.append(f"{l1}\n{l2}")
        else:
            lines.append(' '.join(syllables))
        lines.append("")

    return "\n".join(lines)


def lrc_word_groups(syllables, word_starts):
    if not word_starts or len(word_starts) != len(syllables):
        return None
    groups = []
    cur = []
    for i, (syl, ws) in enumerate(zip(syllables, word_starts)):
        if ws and cur:
            groups.append(cur)
            cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    return groups


def lrc_context_text(syllables, groups, i, ph_seq=None, ph_num=None):
    word_texts = [''.join(syllables[j] for j in g) for g in groups]
    # Deduped word texts (collapse consecutive duplicate syllables per word)
    deduped_word_texts = []
    deduped_groups = []
    for g in groups:
        dg = []
        for si in g:
            if not dg or syllables[si] != syllables[dg[-1]]:
                dg.append(si)
        deduped_groups.append(dg)
        deduped_word_texts.append(''.join(syllables[si] for si in dg))

    for wi, indices in enumerate(groups):
        if i not in indices:
            continue
        # ── line 1: textual (deduped display) ──
        dg = deduped_groups[wi]
        try:
            deduped_pos = dg.index(i)
        except ValueError:
            deduped_pos = next(idx for idx, si in enumerate(dg) if syllables[si] == syllables[i])
        parts = []
        for idx, si in enumerate(dg):
            if idx == deduped_pos:
                parts.append(f"*{syllables[si]}*")
            else:
                parts.append(syllables[si])
        curr = ''.join(parts)
        prev_w = ' '.join(deduped_word_texts[max(0, wi - 2):wi]) if wi > 0 else None
        next_w = ' '.join(deduped_word_texts[wi + 1:min(len(groups), wi + 3)]) if wi < len(groups) - 1 else None

        if prev_w:
            line1 = f"{prev_w} {curr}{' ' + next_w if next_w else ''}"
        else:
            line1 = f"{curr}{' ' + next_w if next_w else ''}"

        # ── line 2: phonetic ──
        if ph_seq and ph_num:
            ph_idx2 = sum(ph_num[:indices[0]]) if indices else 0
            curr_ph_parts = []
            for si in indices:
                num_ph = ph_num[si] if si < len(ph_num) else 0
                phs = []
                for k in range(num_ph):
                    p = ph_seq[ph_idx2] if ph_idx2 < len(ph_seq) else ''
                    p_clean = p.split('/')[-1] if p not in ('SP', 'AP') else None
                    if p_clean:
                        phs.append(p_clean)
                    ph_idx2 += 1
                syl_ph = ''.join(phs)
                if si == i:
                    curr_ph_parts.append(f"*{syl_ph}*")
                else:
                    curr_ph_parts.append(syl_ph)
            curr_ph = ''.join(curr_ph_parts)

            ph_idx2 = 0
            word_ph_texts = []
            for indices_g in groups:
                wph = []
                for si in indices_g:
                    num_ph = ph_num[si] if si < len(ph_num) else 0
                    for k in range(num_ph):
                        p = ph_seq[ph_idx2] if ph_idx2 < len(ph_seq) else ''
                        p_clean = p.split('/')[-1] if p not in ('SP', 'AP') else None
                        if p_clean:
                            wph.append(p_clean)
                        ph_idx2 += 1
                word_ph_texts.append(''.join(wph))

            prev_ph = ' '.join(word_ph_texts[max(0, wi - 2):wi]) if wi > 0 else None
            next_ph = ' '.join(word_ph_texts[wi + 1:min(len(groups), wi + 3)]) if wi < len(groups) - 1 else None

            if prev_ph:
                line2 = f"{prev_ph} {curr_ph}{' ' + next_ph if next_ph else ''}"
            else:
                line2 = f"{curr_ph}{' ' + next_ph if next_ph else ''}"
        else:
            line2 = line1

        return line1, line2
    return word_texts[0] if word_texts else '', ''


def ds_to_lrc(ds_path, lrc_path=None, title=None):
    with open(ds_path, encoding='utf-8') as f:
        segments = json.load(f)
    seg = segments[0]
    text = seg['text']
    syllables = text.split()
    note_durs = [float(x) for x in seg['note_dur'].split()]
    ws_raw = seg.get('word_starts', '')
    word_starts = [c == '1' for c in ws_raw.split()] if ws_raw else None
    ph_num = [int(x) for x in seg.get('ph_num', '').split()] if seg.get('ph_num') else None
    ph_seq = seg.get('ph_seq', '').split() if seg.get('ph_seq') else None

    n = min(len(syllables), len(note_durs))
    syllables = syllables[:n]
    note_durs = note_durs[:n]
    if word_starts and len(word_starts) > n:
        word_starts = word_starts[:n]
    if ph_num and len(ph_num) > n:
        ph_num = ph_num[:n]
        ph_seq = ph_seq[:sum(ph_num)]

    groups = lrc_word_groups(syllables, word_starts)

    t = 0.0
    timings = []
    for i, dur in enumerate(note_durs):
        if groups:
            l1, l2 = lrc_context_text(syllables, groups, i, ph_seq, ph_num)
            timings.append((t, f"{l1}\n{l2}"))
        else:
            timings.append((t, ' '.join(syllables)))
        t = round(t + dur, 2)

    lrc = gen_lrc(timings, title=title)
    if lrc_path:
        Path(lrc_path).write_text(lrc, encoding='utf-8')
        return lrc_path
    return lrc


# ---------------------------------------------------------------------------
# DS rescaling (inlined).
# ---------------------------------------------------------------------------
NOTE_IDX = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def name_to_midi(name):
    if "#" in name:
        return 12 * (int(name[2:]) + 1) + NOTE_IDX[name[0]] + 1
    return 12 * (int(name[1:]) + 1) + NOTE_IDX[name[0]]


def rescale_segment(seg, factor):
    note_dur = [float(x) for x in seg["note_dur"].split()]
    ph_dur = [float(x) for x in seg["ph_dur"].split()]
    note_seq = seg["note_seq"].split()

    note_dur_s = [x * factor for x in note_dur]
    ph_dur_s = [x * factor for x in ph_dur]

    ft = float(seg["f0_timestep"])
    f0 = []
    for name, dur in zip(note_seq, note_dur_s):
        freq = 440.0 * (2.0 ** ((name_to_midi(name) - 69) / 12.0))
        nf = max(1, int(dur / ft))
        f0.extend([f"{freq:.2f}"] * nf)

    seg2 = dict(seg)
    seg2["note_dur"] = " ".join(f"{x:.6f}" for x in note_dur_s)
    seg2["ph_dur"] = " ".join(f"{x:.6f}" for x in ph_dur_s)
    seg2["f0_seq"] = " ".join(f0)
    return seg2


# ---------------------------------------------------------------------------
# MIDI / organ (inlined).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# MIDI writer (stdlib only). It writes the same musical content as the old
# midiutil-based generator: 960 ticks/quarter, tempo 107 for song.mid, program
# 74, one text event per syllable, note-off velocity 127.
# ---------------------------------------------------------------------------
class MidiWriter:
    def __init__(self, ticks_per_quarter=960):
        self.ticks = ticks_per_quarter
        self.events = []
        self.order = 0

    def _add_ticks(self, tick, data, priority=50):
        self.events.append((int(tick), priority, self.order, data))
        self.order += 1

    def _add(self, tick_beats, data, priority=50):
        self._add_ticks(int(round(tick_beats * self.ticks)), data, priority)

    def meta(self, tick_beats, meta_type, payload, priority=50):
        self._add(tick_beats, bytes([0xFF, meta_type, len(payload)]) + payload, priority)

    def track_name(self, tick_beats, name):
        self.meta(tick_beats, 0x03, _sanitize_latin1(name).encode('latin-1', 'replace'), 0)

    def tempo(self, tick_beats, bpm):
        if bpm <= 0:
            return
        us = int(60000000.0 / bpm)
        if us < 1:
            us = 1
        data = bytes([0xFF, 0x51, 0x03]) + us.to_bytes(3, 'big')
        tick = int(round(tick_beats * self.ticks))
        # midiutil removes identical tempo events at the same tick; do the same.
        for t, _p, _o, d in self.events:
            if t == tick and d == data:
                return
        self.events.append((tick, 4, self.order, data))
        self.order += 1

    def program(self, tick_beats, channel, program):
        self._add(tick_beats, bytes([0xC0 | (channel & 0x0F), program & 0x7F]), 1)

    def note_on(self, tick_beats, channel, pitch, velocity=127):
        self._add(tick_beats, bytes([0x90 | (channel & 0x0F), pitch & 0x7F, velocity & 0x7F]), 5)

    def note_off(self, tick_beats, channel, pitch, velocity=127):
        self._add(tick_beats, bytes([0x80 | (channel & 0x0F), pitch & 0x7F, velocity & 0x7F]), 3)

    def text(self, tick_beats, text):
        self.meta(tick_beats, 0x01, _sanitize_latin1(text).encode('latin-1', 'replace'), 2)

    @staticmethod
    def _vlq(n):
        out = [n & 0x7F]
        n >>= 7
        while n:
            out.append((n & 0x7F) | 0x80)
            n >>= 7
        return bytes(reversed(out))

    def write(self, path):
        events = sorted(self.events, key=lambda e: (e[0], e[1], e[2]))
        track = bytearray()
        last_tick = 0
        for tick, _priority, _order, data in events:
            track += self._vlq(tick - last_tick)
            track += data
            last_tick = tick
        track += self._vlq(0) + b'\xFF\x2F\x00'
        header = (
            b'MThd' + (6).to_bytes(4, 'big') + (0).to_bytes(2, 'big') +
            (1).to_bytes(2, 'big') + self.ticks.to_bytes(2, 'big')
        )
        Path(path).write_bytes(
            header + b'MTrk' + len(track).to_bytes(4, 'big') + bytes(track)
        )


def notes_to_midi(notes, output_path, tempo=107, title="Gregorian",
                  transposition=None, metadata=None, pitch_shift=0):
    """Write a format-0 MIDI file from parsed GABC notes."""
    if transposition is None:
        transposition = _auto_transposition(notes)

    midi = MidiWriter()
    tempo_int = tempo / 2.0
    time_beats = 0.0

    track_name = title
    if metadata:
        parts = [title]
        op = metadata.get('office-part', '')
        md = metadata.get('mode', '')
        bk = metadata.get('book', '')
        cm = metadata.get('commentary', '')
        if op:
            parts.append(op)
        if md:
            parts.append(md)
        if bk:
            parts.append(bk[:60])
        if cm:
            parts.append(cm[:60])
        track_name = ' | '.join(parts)

    midi.track_name(0, track_name)
    midi.tempo(0, tempo_int)
    midi.program(0, 0, 74)

    for i, n in enumerate(notes):
        pitch = n.pitch + transposition + pitch_shift
        pitch = max(0, min(127, pitch))
        dint = int(n.duration)
        midi_dur = dint / 2.0
        actual_tempo = tempo_int * dint / n.duration if n.duration > 0 else tempo_int

        midi.tempo(time_beats, actual_tempo)
        midi.note_on(time_beats, 0, pitch, 127)
        if n.lyric:
            has_next = any(m.lyric for m in notes[i + 1:])
            syl = n.lyric
            if n.is_word_start:
                syl = ' ' + n.lyric
            if has_next:
                syl = syl + ' '
            midi.text(time_beats, syl)
        midi.note_off(time_beats + midi_dur, 0, pitch, 127)

        time_beats += midi_dur

    midi.write(output_path)
    return Path(output_path)


def midi_to_wav_organ(midi_path, output_path=None, soundfont=None, gain_db=0, fluidsynth=None):
    mp = Path(midi_path)
    sf = Path(soundfont or SOUNDFONT)
    if not sf.is_absolute():
        sf = (DB_ROOT / sf).resolve()
    fs = fluidsynth or FLUIDSYNTH
    if not sf.exists():
        raise FileNotFoundError(f"SoundFont não encontrado: {sf}")

    if output_path is None:
        output_path = mp.with_suffix(".wav")
    op = Path(output_path)
    op.parent.mkdir(parents=True, exist_ok=True)

    cmd = [str(fs), "-F", str(op), "-i", str(sf), str(mp)]
    if gain_db:
        cmd = [str(fs), "-F", str(op), "-i", "-g", str(gain_db), str(sf), str(mp)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERRO fluidsynth: {result.stderr}", file=sys.stderr)
        result.check_returncode()
    return op


def header_meta(raw: str, meta: dict) -> dict:
    """Completa `office-part`/`mode`/`book`… de arquivos com DOIS cabeçalhos.

    Formato do GregoBase: o arquivo pode ter

        name:Venit ad Petrum;      ← 1º bloco: só o nome de exibição
        %%
        name: v ;                  ← 2º bloco: metadados (aqui `name` é um slug!)
        office-part:...; mode:...; book:...;
        %%
        (notação)

    O `_parse_metadata` só olha o texto antes do 1º `%%`, então nesses arquivos
    ele devolve nome vazio e mais nada — era o que deixava 48 das 93 músicas do
    1º lote sem `office` e 17 sem `mode`.

    Regra: cada chave **só é preenchida se o parser não achou** nada, e vale a
    ÚLTIMA ocorrência não vazia no topo do arquivo (a do 2º bloco, que é o
    cabeçalho completo). O `name` NUNCA é tocado aqui: o 2º bloco traz um slug
    (`name: v ;`) e usá-lo batizaria a música de "v" — quem cuida do nome é
    `header_name()`, que fica com a PRIMEIRA ocorrência (a de exibição).
    """
    out = dict(meta)
    head = raw[:HDR_SCAN]
    for k in HDR_KEYS:
        if k == "name" or (out.get(k) or "").strip():
            continue                     # já veio do parser (ou é regra do name)
        vals = [v.strip() for v in re.findall(
            r"(?mi)^[ \t]*" + re.escape(k) + r"[ \t]*:[ \t]*([^;\r\n]+)", head)]
        vals = [v for v in vals if v]
        if vals:
            out[k] = vals[-1]
    return out


HDR_KEYS = ("name", "book", "office-part", "mode", "commentary", "transcriber")
HDR_SCAN = 8192

def header_name(raw: str, meta: dict, stem: str) -> str:
    """`name` de exibição. Fica com a PRIMEIRA ocorrência não vazia (o 1º bloco
    do GregoBase). O `----.gabc`, cujo 1º bloco tem `name:;` vazio, cai no 2º.
    """
    name = (meta.get("name") or "").strip()
    if name:
        return name
    for m in re.finditer(r"(?mi)^\s*name\s*:\s*([^;\r\n]+)", raw):
        v = m.group(1).strip()
        if v:
            return v
    return stem


def notes_and_text(raw: str):
    """Notas do parser + 1 sílaba por nota (preenche as notas sem letra)."""
    notes = GabcParser().parse(raw)
    toks, ws = [], []
    for n in notes:
        l = n.lyric.strip("-.").strip().lower() if n.lyric else ""
        toks.append(re.sub(r"[.,;:*<>]+", "", l).strip())
        ws.append(bool(n.is_word_start))
    prev = ""
    for i, t in enumerate(toks):        # 1ª passada: herda a anterior
        if t:
            prev = t
        else:
            toks[i] = prev
    nxt = ""
    for i in range(len(toks) - 1, -1, -1):   # 2ª: quem ficou no começo herda a próxima
        if toks[i]:
            nxt = toks[i]
        else:
            toks[i] = nxt
    toks = [t or "\u2026" for t in toks]
    return notes, " ".join(toks), [n.pitch for n in notes], [n.duration for n in notes], ws


def make_game_data(ds_path: Path, out_path: Path):
    """Converte um .ds no game_data.json que o pitch_game.js espera (igual ao pipeline)."""
    seg = json.loads(ds_path.read_text(encoding="utf-8"))[0]
    note_dur = [float(x) for x in seg["note_dur"].split()]
    ws = [c == "1" for c in seg["word_starts"].split()] if seg.get("word_starts") else None
    f0 = [float(x) for x in seg["f0_seq"].split()]
    data = {
        "text": seg["text"].split(),
        "note_seq": seg["note_seq"].split(),
        "note_dur": [f"{x:.6f}" for x in note_dur],
        "word_starts": ws,
        "f0": f0,
        "f0_timestep": float(seg["f0_timestep"]),
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return len(note_dur), len(data["text"])


def to_opus_mono(wav: Path, opus: Path, kbps: int, title: str, vbr: str = "off", ffmpeg=None):
    """wav → opus mono 48 kHz. `vbr="off"` (default) = CBR: o `-b:a` é o que sai
    de verdade no arquivo. `vbr="on"` deixa o órgão gastar bem acima do alvo."""
    cmd = [str(ffmpeg or FFMPEG), "-y", "-v", "error", "-i", str(wav),
           "-ac", "1", "-ar", "48000", "-c:a", "libopus",
           "-b:a", f"{kbps}k", "-vbr", vbr,
           "-metadata", f"title={title}", str(opus)]
    subprocess.run(cmd, check=True)
    return opus


def wav_dur(p: Path):
    with wave.open(str(p)) as w:
        return w.getnframes() / w.getframerate()


# ---------------------------------------------------------------------------
# Helpers shared by the seven CLI scripts.
# ---------------------------------------------------------------------------
def find_song(song_id):
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for song in data:
        if song.get("id") == song_id:
            return song
    raise SystemExit(f"song not found in songs.json: {song_id}")


def song_dir(song_id):
    song = find_song(song_id)
    rel = song.get("dir", "")
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "database":
        raise SystemExit(f"unexpected dir in songs.json: {rel!r}")
    return DB_ROOT.joinpath(*parts[1:])


def song_gabc(song_id):
    book = song_dir(song_id).relative_to(DB_ROOT).parts[0]
    return GABC_DIR / book / f"{song_id}.gabc"


def resolve_cli_inputs(args, parser):
    if args.song:
        if args.gabc:
            parser.error("use either GABC or --song, not both")
        args.gabc = str(song_gabc(args.song))
        if not args.out:
            args.out = str(song_dir(args.song))
    if not args.gabc:
        parser.error("GABC or --song is required")
    if not args.out:
        args.out = str(Path.cwd())
    gabc = Path(args.gabc)
    if not gabc.is_file():
        parser.error(f"GABC not found: {gabc}")
    if gabc.suffix.lower() != ".gabc":
        parser.error(f"not a .gabc file: {gabc}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    return gabc, out


def original_gabc_stem(path):
    """Recover the original GregoBase stem when the GABC header has no name.

    The files in cc0-1.0/gabc/<book> are named <song-id>.gabc. For a few stubs
    (e.g. solesmes-1934, whose source is ----solesmes_1934.gabc) the header has
    an empty name, and the historical title is the ORIGINAL file stem, not the
    song id. SOURCES.tsv keeps that mapping.
    """
    p = Path(path)
    index = GABC_DIR / "SOURCES.tsv"
    if index.is_file():
        for line in index.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split("	", 1)
            if len(parts) == 2 and parts[0] == p.stem:
                return Path(parts[1]).stem
    return p.stem


def read_gabc(path):
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    meta = header_meta(raw, _parse_metadata(raw))
    name = header_name(raw, meta, original_gabc_stem(p))
    notes, text, pitches, durations, word_starts = notes_and_text(raw)
    if not notes:
        raise SystemExit(f"no notes in {path}")
    return raw, meta, name, notes, text, pitches, durations, word_starts


def write_ds(gabc_path, ds_path):
    """Generate song.ds and return (context, ds_path)."""
    raw, meta, name, notes, text, pitches, durations, word_starts = read_gabc(gabc_path)
    ds_path = Path(ds_path)
    ds_path.parent.mkdir(parents=True, exist_ok=True)
    dummy_mid = ds_path.with_suffix(".mid")  # only a placeholder; not read
    mid_to_ds(
        str(dummy_mid), text, str(ds_path), "pt",
        pitches=pitches, durations=durations,
        word_starts=word_starts, word_silence=WORD_SILENCE,
    )
    return (raw, meta, name, notes, text, pitches, durations, word_starts), ds_path


def write_organ_mid(notes, out_path, name, meta):
    return notes_to_midi(
        notes, out_path, tempo=TEMPO_MID, title=name,
        transposition=0, metadata=meta, pitch_shift=ORGAN_PITCH,
    )


def main():
    p = argparse.ArgumentParser(description="GABC -> song.srt")
    p.add_argument("gabc", nargs="?", help="input .gabc")
    p.add_argument("--song", help="song id from songs.json")
    p.add_argument("-o", "--out", help="output directory")
    args = p.parse_args()
    gabc, out = resolve_cli_inputs(args, p)
    with tempfile.TemporaryDirectory(prefix="gabc_srt_") as td:
        _ctx, tmp_ds = write_ds(gabc, Path(td) / "song.ds")
        (out / "song.srt").write_text(ds_to_srt(str(tmp_ds)), encoding="utf-8")
    print(f"[song.srt] {out / 'song.srt'}")

if __name__ == "__main__":
    main()
