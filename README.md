# AyahReel audio mirror

A copy of the recitation files the [AyahReel](https://github.com/Mulhimfy)
app plays, so a reel can still be made when the site a recitation comes
from is down or can't be reached.

Each voice is one release, `voice-<id>`:

| Asset | What it is |
|---|---|
| `p<n>-<sha>.bin` | The voice's files exactly as their hosts serve them, one after another |
| `index.json` | Where each file is in the packs: `files: {key: [pack, offset, length]}` |
| `sums.json` | The sha256 of each file as it came from its host |
| `timing.json` | For voices of one file per surah: the verse table the host publishes, verse starts and ends only |

Keys are `SSSAAA` (surah, verse) for voices of one file per verse and
`SSS` for voices of one file per surah. A file is read with a Range
request on its pack, so what comes back is the host's own file, byte for
byte.

`voices.json` lists every voice and the address of every file and table
it was copied from. The recitations belong to their reciters and the
sites that publish them: [EveryAyah](https://everyayah.com),
[mp3quran.net](https://mp3quran.net), [QuranicAudio](https://quranicaudio.com),
[Tarteel](https://tarteel.ai), [King Saud University](https://quran.ksu.edu.sa),
[archive.org](https://archive.org) and [Way2Quran](https://way2quran.com).

`mirror.yml` (started by hand) copies the listed voices and reads every
pack back before it publishes the index; `mode: check` reads every file
of a voice back by range against `sums.json`.
