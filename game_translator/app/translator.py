"""Translate game text with the Claude API."""
from __future__ import annotations

import base64
import json
import re

import cv2
import numpy as np

SKIP = "<skip>"

SYSTEM_PROMPT = """You translate video game text from English to {language} for a live subtitle overlay.

The English text comes from OCR on a compressed video stream, so it may contain recognition errors \
(wrong letters, missing spaces, stray symbols). Silently work out the intended English first, then translate it.

Rules:
- Output only the translation. No quotes, notes, alternatives or explanations.
- Use natural, everyday {language} that fits game dialogue. Keep the tone: slang, jokes and attitude.
- {names_rule}
- If the text starts with a speaker name and a colon, keep it as a name and translate the rest.
- The text may mark coloured words with tags like <c1>safehouse</c1>. Keep every tag pair, \
wrapped around the words with the same meaning in your translation. Never add new tags.
- <i1/>, <i2/> … are controller buttons or keys drawn as icons. Keep each token exactly once, \
where it belongs in the {language} sentence (e.g. "Press <i1/> to eat" -> "اضغط <i1/> لتأكل").
- Menu items, options and button labels (Select, Back, Resume, Settings…): use the short words \
a real game in {language} would use.
- If the input is not real text (random letters, symbols, cut-off noise), output exactly {skip}"""

NAMES_LATIN = "Keep names of people, places, brands, vehicles and weapons in their original Latin spelling."
NAMES_ARABIC = (
    "Write names of people, places and businesses in the target script by sound "
    "(e.g. Lucia -> لوسيا, Chuck's Gun Shop -> تشوكس غن شوب). Keep vehicle and weapon model names in Latin letters."
)

PAGE_PROMPT = """These are all the texts on one game screen ({hint}), as a JSON object of id -> text.
Translate every value.
- Keep each translation about as long as the English so it fits the same space on screen.
- If an English text is cut off (ends mid-word or with ...), translate what is there and end with …
- Headings and menu items: short, like a real app in {language}.
Return only a JSON object with the same ids. No other text.

{items}"""

AREA_DESCRIPTIONS = {
    "subtitles": "a line of dialogue subtitles",
    "instructions": "an on-screen mission instruction or hint",
}


class ClaudeTranslator:
    def __init__(self, api_key: str, model: str, target_language: str, transliterate_names: bool = True) -> None:
        import anthropic

        # api_key=None makes the SDK read ANTHROPIC_API_KEY from the environment.
        self._client = anthropic.Anthropic(api_key=api_key or None, timeout=15.0, max_retries=1)
        self.model = model
        self.target_language = target_language
        names_rule = NAMES_ARABIC if transliterate_names else NAMES_LATIN
        if not target_language.lower().startswith("arab"):
            # the examples in the prompt are Arabic; keep them only as a format hint
            names_rule = names_rule.replace("target script", f"{target_language} script")
        self._system = SYSTEM_PROMPT.format(language=target_language, skip=SKIP, names_rule=names_rule)

    def translate_text(self, text: str, area: str, context: list[str]) -> str | None:
        prompt = self._context_block(context)
        prompt += f"Translate this {AREA_DESCRIPTIONS.get(area, 'game text')}:\n{text}"
        return self._call([{"type": "text", "text": prompt}])

    def translate_image(self, crop_bgr: np.ndarray, area: str, ocr_guess: str, context: list[str]) -> str | None:
        """Send the picture itself, for when OCR is unsure."""
        ok, png = cv2.imencode(".png", crop_bgr)
        if not ok:
            return self.translate_text(ocr_guess, area, context)
        prompt = self._context_block(context)
        prompt += (
            f"The image is a crop of the game screen showing {AREA_DESCRIPTIONS.get(area, 'game text')}. "
            f"Read the English text in it and translate it.\n"
            f"An OCR program guessed: {ocr_guess!r} (may be wrong).\n"
            f"If there is no readable text, output {SKIP}"
        )
        return self._call([
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(png.tobytes()).decode("ascii"),
                },
            },
            {"type": "text", "text": prompt},
        ])

    def translate_page(self, items: dict[str, str], hint: str, model: str | None = None) -> dict[str, str]:
        """Translate all texts of one screen in a single call. items: id -> English (may hold <cN> tags)."""
        prompt = PAGE_PROMPT.format(
            hint=hint, language=self.target_language, items=json.dumps(items, ensure_ascii=False, indent=0)
        )
        response = self._client.messages.create(
            model=model or self.model,
            max_tokens=4000,
            system=self._system,
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v).strip() for k, v in data.items() if str(k) in items and str(v).strip()}

    @staticmethod
    def _context_block(context: list[str]) -> str:
        if not context:
            return ""
        lines = "\n".join(f"- {line}" for line in context)
        return f"Previous dialogue, for context only (do not translate it):\n{lines}\n\n"

    def _call(self, content: list[dict]) -> str | None:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=500,
            system=self._system,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()
        if not text or SKIP in text:
            return None
        return text
