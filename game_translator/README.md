# Game Translator (draft v0.4)

Reads English subtitles and instructions from a game window (PS Remote Play), translates them with the
Claude API and draws the translation on top of the game. Every translation is saved, so the same line is
never sent to Claude twice.

## Run

1. Install **Python 3.12** from python.org (tick "Add python.exe to PATH").
2. Double-click `run.bat`. The first run installs the packages (a few minutes).
3. **Source** tab: pick `PS Remote Play`.
4. **Translator** tab: paste your Claude API key, press **Test translation**.
5. **Read areas** tab: press **Take snapshot and draw areas** while a subtitle is on screen and fix the boxes.
6. Press **Start**.

## Make it a normal program (GameTranslator.exe)

Double-click `build_exe.bat` once (needs Python only for this step). It creates
`dist\GameTranslator\GameTranslator.exe`: copy that folder anywhere and open the .exe like any program.
Settings and saved translations are kept next to the .exe.

## How it works

```
capture window (mss) ──► crop read areas ──► picture changed? ──► OCR (RapidOCR)
        ▲                                                            │
        │                                       same text 2 frames in a row?
        │                                                            │
   every 250 ms                         memory (SQLite) hit? ──yes──► overlay
                                                 │ no
                                OCR sure? ──yes──► Claude (text)  ──► save ──► overlay
                                     │ no
                                     └────────► Claude (picture) ──► save ──► overlay
```

## Phone and menus (pages)

The `phone` read area is a *page*: every text on it is read with its box, colour and weight.
The screen is copied, flipped right-to-left, the English is erased and the Arabic is drawn in the
same places (avatars, icons and the selection frame stay). The Arabic screen is drawn beside the
real phone (or over it, Show on tab).

- While the screen moves (scrolling), only saved translations are used — no API calls.
- When it stops, all missing texts of the screen go to Claude in ONE call.
- Words in another colour (e.g. a yellow place name) are tagged `<c1>…</c1>` and keep their colour.
- Controller / key icons inside a sentence ("Press ⓧ to eat") become tokens `<i1/>`: Claude keeps them in
  the right place in the Arabic sentence and the real icon picture is drawn back there.

The `menu` page covers the whole screen and wakes up when 6+ texts are visible (pause menu, map, shop,
settings, the Select / Back button bar). It keeps the game's layout and replaces the English in place.
While it is open, subtitles are not read. Turn it off in Show on → "Translate menus…".

## Files

| File | What it does |
|---|---|
| `main.py` | Starts the app |
| `app/config.py` | All settings, saved to `config.json` |
| `app/capture.py` | Finds the window, grabs its picture, hides the overlay from capture |
| `app/ocr.py` | Upscales + sharpens the crop, runs OCR, groups words into lines |
| `app/translator.py` | Claude prompt for text and for pictures |
| `app/memory.py` | SQLite memory: `data/translations.db` |
| `app/pipeline.py` | The main loop (runs in a background thread) |
| `app/pages.py` | Phone / menu screens: read blocks, erase, redraw in Arabic |
| `app/styles.py` | Text colour, background, bold, coloured-word tags |
| `app/overlay.py` | Transparent click-through window over the game |
| `app/area_picker.py` | Dialog to draw read areas on a snapshot |
| `app/main_window.py` | The main window and its tabs |

## Tuning

- `capture_interval_ms` (config.json): lower = faster reaction, more CPU.
- `stable_frames`: how many identical readings before translating. 2 is a good balance.
- `vision_confidence_threshold`: higher = sends the picture to Claude more often (better on blur, costs more).
- Models: `claude-opus-5-5` is the default (best quality). `claude-haiku-5-5` is much faster for subtitles.

## Show on a TV

Install the Android TV app (folder `game_translator_tv`, see its README), open it on the TV, then
**Show on → Connect a TV** and type the 6-digit code. Every translation (subtitles, instructions,
phone, menus) is drawn on the PC as a picture and sent to the TV over Wi-Fi; the TV draws it over the
console picture. The TV is remembered and reconnects by itself. **Send a test** shows a line on the TV.
If Windows asks whether Python may use the network, allow it on private networks.

## Later

- Per-game profiles, follow the phone when it moves in the hand, Samsung / LG TV apps.
