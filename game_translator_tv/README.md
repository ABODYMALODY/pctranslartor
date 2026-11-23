# Game Translator — Android TV app (v0.1)

Draws the translations from the Game Translator PC program over the console picture on an
Android TV / Google TV (e.g. TCL). The TV only draws; reading and translating happen on the PC.

```
PlayStation ──HDMI──► TV  (picture, no delay)
     │
 Remote Play / capture card
     ▼
    PC: read → translate (Claude) → draw ──Wi-Fi──► TV app: shows the pictures on top
```

## Build the APK (once)

1. Install **Android Studio** (free) from developer.android.com/studio.
2. **File → Open** → pick this `game_translator_tv` folder. Wait for "Gradle sync" to finish
   (first time downloads a lot). If it offers to update the Android Gradle Plugin, you can say no.
3. **Build → Build App Bundle(s) / APK(s) → Build APK(s)**.
   The file is `app/build/outputs/apk/debug/app-debug.apk`.

## Install on the TV (ADB over Wi-Fi — also lets you reinstall in one click later)

1. TV: **Settings → System → About** → click **Android TV OS build** 7 times (Developer options on).
2. TV: **Settings → System → Developer options** → turn on **USB debugging**
   (and **Network debugging / Wireless debugging** if the TV has it).
3. Open the app once with any method, or read the TV's IP in **Settings → Network**.
4. PC (Android Studio's terminal):
   ```
   adb connect <TV IP>:5555
   adb install -r app/build/outputs/apk/debug/app-debug.apk
   ```
   Accept the "Allow USB debugging?" question on the TV.
   After that, Android Studio's **Run ▶** button installs straight to the TV.

## First start on the TV

1. Open **Game Translator** from the TV's apps.
2. Press **Allow drawing over the TV picture** → turn it on for Game Translator → Back.
   If the TV has no such page: `adb shell appops set com.gametranslator.tv SYSTEM_ALERT_WINDOW allow`
3. The app shows a 6-digit code. On the PC: **Show on → Connect a TV** → type the code.
4. Press **Home**, switch to the PlayStation's HDMI input, play. The app keeps running in the background.

## How it works

| File | What it does |
|---|---|
| `LinkServer.kt` | Answers the PC's search for the code (UDP 47800), receives pictures (TCP 47801), remembers paired PCs |
| `OverlayService.kt` | Runs in the background so it keeps working over the HDMI input |
| `OverlayLayers.kt` | Transparent full-screen window; each group (subtitles, phone, menu) is a set of pictures |
| `MainActivity.kt` | The screen with the code and the connection status |

Protocol: one JSON object per line. `{"type":"group","id":"subtitles","layers":[{"x","y","w","h","png"}]}`
replaces what the TV shows for that group (x, y, w, h are fractions of the screen, png is base64).
An empty list clears it. `{"type":"clear_all"}` clears everything.
