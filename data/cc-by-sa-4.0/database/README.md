# Song generators

Seven self-contained scripts regenerate the files in each song directory from
the CC0 GABC source in `../../cc0-1.0/gabc/<book>/<song-id>.gabc`.

| Script | Output |
|---|---|
| `gabc_to_game_datajson.py` | `game_data.json` |
| `gabc_to_organopus.py` | `organ.opus` |
| `gabc_to_song_ds.py` | `song.ds` |
| `gabc_to_song_lrc.py` | `song.lrc` |
| `gabc_to_song_mid.py` | `song.mid` |
| `gabc_to_song_srt.py` | `song.srt` |
| `gabc_to_song_organ_ds.py` | `song_organ.ds` |

Each `.py` contains its own copy of the GABC parser, DS generator, subtitle
code and MIDI writer. They do not import one another or any project module.

## Usage

From this directory:

```bash
python3 gabc_to_song_ds.py --song pater-noster-a
python3 gabc_to_song_mid.py --song pater-noster-a
```

`--song` finds the GABC and the output song folder automatically. You can also
pass a GABC path and an output directory explicitly:

```bash
python3 gabc_to_song_ds.py ../../cc0-1.0/gabc/graduale_romanum/pater-noster-a.gabc \
  -o graduale_romanum/pater-noster-a
```

To regenerate all seven files for one song:

```bash
for script in gabc_to_*.py; do
  python3 "$script" --song pater-noster-a
done
```

## External dependencies

Only the organ scripts need external programs:

- `gabc_to_organopus.py`: `fluidsynth`, `ffmpeg` with libopus, and an organ
  `.sf2` SoundFont.
- `gabc_to_song_organ_ds.py`: `fluidsynth` and the same `.sf2` (it renders a
  temporary organ WAV to measure the exact duration).

Defaults and overrides are documented in each script header. The other five
scripts use the Python standard library only.

## GABC source

`data/cc0-1.0/gabc/` contains the 18,720 source transcriptions, organized in
the same `<book>/` folders as `database/` and named `<book>/<song-id>.gabc`.
`SOURCES.tsv` maps each one back to its original GregoBase filename.
