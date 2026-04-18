import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import speech_recognition as sr

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Speed presets
# ---------------------------------------------------------------------------
SPEED_RATES: Dict[str, int] = {
    "slow": settings.voice.speed_slow,
    "medium": settings.voice.speed_medium,
    "fast": settings.voice.speed_fast,
}

# ---------------------------------------------------------------------------
# Voice command vocabulary  (canonical_command → recognized_phrases)
# ---------------------------------------------------------------------------
COMMAND_VOCAB: Dict[str, List[str]] = {
    "next":     ["next", "அடுத்து", "அடுத்தது", "forward"],
    "previous": ["previous", "back", "முந்தைய", "பின்", "prev"],
    "repeat":   ["repeat", "again", "மீண்டும்", "மறுபடியும்"],
    "explain":  ["explain", "விளக்கு", "விளக்கம்", "details"],
    "skip":     ["skip", "தவிர்", "pass"],
    "bookmark": ["bookmark", "save", "குறிப்பு", "mark"],
    "menu":     ["menu", "home", "பட்டியல்", "main menu"],
    "yes":      ["yes", "yeah", "yep", "ok", "okay", "sure",
                 "ஆம்", "சரி", "ஒரே"],
    "no":       ["no", "nope", "nah", "cancel",
                 "இல்லை", "வேண்டாம்", "வேண்டாம"],
    "slow":     ["slow", "slower", "மெதுவாக", "slowly"],
    "fast":     ["fast", "faster", "வேகமாக", "speed up"],
    "medium":   ["medium", "normal", "நடுத்தர"],
    "stop":     ["stop", "quit", "exit", "bye", "நிறுத்து", "விடு"],
    "help":     ["help", "உதவி", "commands"],
}

# Number words → digit (English + Tamil)
NUMBER_WORDS: Dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "ஒன்று": 1, "இரண்டு": 2, "மூன்று": 3, "நான்கு": 4, "ஐந்து": 5,
    "ஆறு": 6, "ஏழு": 7, "எட்டு": 8, "ஒன்பது": 9, "பத்து": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}

# Tamil Unicode block range
_TAMIL_START = 0x0B80
_TAMIL_END = 0x0BFF

# Sentence-splitting regex (handles Tamil punctuation too)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?।])\s+")

# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def detect_language(text: str) -> str:
    if not text or not text.strip():
        return "english"

    tamil_chars = sum(1 for ch in text if _TAMIL_START <= ord(ch) <= _TAMIL_END)
    total_alpha = sum(1 for ch in text if ch.isalpha())
    if total_alpha == 0:
        return "english"

    tamil_ratio = tamil_chars / total_alpha

    if tamil_ratio > 0.6:
        return "tamil"
    if tamil_ratio > 0.15:
        return "mixed"

    try:
        from langdetect import detect as _ld_detect
        lang = _ld_detect(text)
        if lang == "ta":
            return "tamil"
    except Exception:
        pass

    return "english"


# ---------------------------------------------------------------------------
# Audio playback helper (cross-platform, no hard pygame dep for mp3)
# ---------------------------------------------------------------------------

def _play_mp3(path: str) -> bool:
    try:
        import pygame
        pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        pygame.mixer.music.stop()
        pygame.mixer.quit()
        return True
    except Exception:
        pass

    if sys.platform == "darwin" and shutil.which("afplay"):
        subprocess.run(["afplay", path], check=True, capture_output=True)
        return True

    for player in ("mpg123", "mpg321", "ffplay", "cvlc"):
        if shutil.which(player):
            args = [player]
            if player == "ffplay":
                args += ["-nodisp", "-autoexit", "-loglevel", "quiet"]
            elif player == "cvlc":
                args += ["--play-and-exit", "--quiet"]
            args.append(path)
            subprocess.run(args, check=True, capture_output=True)
            return True

    logger.warning("No mp3 player found. Tamil audio not played.")
    return False


# ---------------------------------------------------------------------------
# TTS engines
# ---------------------------------------------------------------------------

class _PyttxsEngine:
    def __init__(self) -> None:
        self._engine = None

    def _get(self):
        if self._engine is None:
            import pyttsx3
            self._engine = pyttsx3.init()
            self._engine.setProperty("volume", settings.voice.tts_volume)
        return self._engine

    def speak(self, text: str, rate: int) -> None:
        engine = self._get()
        engine.setProperty("rate", rate)
        sentences = _SENTENCE_SPLIT.split(text) or [text]
        for sentence in sentences:
            sentence = sentence.strip()
            if sentence:
                engine.say(sentence)
                engine.runAndWait()
                time.sleep(settings.voice.inter_sentence_pause_ms / 1000)

    def stop(self) -> None:
        if self._engine:
            try:
                self._engine.stop()
            except Exception:
                pass


class _GTTSEngine:
    def speak(self, text: str, slow: bool = False) -> bool:
        try:
            from gtts import gTTS
            tts = gTTS(text=text, lang="ta", tld=settings.voice.gtts_tld, slow=slow)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_path = f.name
            tts.save(tmp_path)
            success = _play_mp3(tmp_path)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return success
        except Exception as e:
            logger.warning(f"gTTS failed: {e}")
            return False


# ---------------------------------------------------------------------------
# STT engines
# ---------------------------------------------------------------------------

class _GoogleSTT:
    def transcribe(
        self, audio: sr.AudioData, lang_code: str
    ) -> Optional[str]:
        recognizer = sr.Recognizer()
        try:
            return recognizer.recognize_google(audio, language=lang_code)
        except sr.UnknownValueError:
            return None
        except Exception as e:
            logger.debug(f"Google STT error: {e}")
            raise


class _VoskSTT:
    def __init__(self) -> None:
        self._models: Dict[str, object] = {}

    def _load_model(self, lang: str):
        if lang in self._models:
            return self._models[lang]
        try:
            from vosk import Model
            base = Path(__file__).resolve().parent.parent
            model_path = str(
                base / (
                    settings.voice.vosk_model_ta
                    if lang == "ta"
                    else settings.voice.vosk_model_en
                )
            )
            if not Path(model_path).exists():
                logger.warning(f"Vosk model not found at {model_path}")
                return None
            model = Model(model_path)
            self._models[lang] = model
            logger.info(f"Vosk model loaded: {model_path}")
            return model
        except ImportError:
            logger.debug("vosk not installed; offline STT unavailable")
            return None
        except Exception as e:
            logger.warning(f"Vosk model load failed: {e}")
            return None

    def transcribe(self, audio: sr.AudioData, lang: str) -> Optional[str]:
        model = self._load_model(lang)
        if model is None:
            return None
        try:
            from vosk import KaldiRecognizer
            rec = KaldiRecognizer(model, 16000)
            wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
            rec.AcceptWaveform(wav_data)
            result = json.loads(rec.FinalResult())
            return result.get("text") or None
        except Exception as e:
            logger.warning(f"Vosk transcription failed: {e}")
            return None


# ---------------------------------------------------------------------------
# Core VoiceModule
# ---------------------------------------------------------------------------

class VoiceModule:
    def __init__(self) -> None:
        self._language: str = settings.language.default_language
        self._speed: str = settings.voice.default_speed
        self._last_spoken: str = ""

        self._recognizer = sr.Recognizer()
        self._recognizer.energy_threshold = settings.voice.energy_threshold
        self._recognizer.pause_threshold = settings.voice.pause_threshold
        self._recognizer.dynamic_energy_threshold = True

        self._pyttsx3 = _PyttxsEngine()
        self._gtts = _GTTSEngine()
        self._google_stt = _GoogleSTT()
        self._vosk_stt = _VoskSTT()

        logger.info("VoiceModule initialised")

    # ------------------------------------------------------------------
    # Public: language / speed
    # ------------------------------------------------------------------

    def set_language(self, language: str) -> None:
        if language not in settings.language.supported_languages:
            raise ValueError(f"Unsupported language: {language}")
        self._language = language

    def set_speed(self, speed: str) -> None:
        if speed not in SPEED_RATES:
            raise ValueError(f"Speed must be one of {list(SPEED_RATES)}")
        self._speed = speed
        logger.info(f"Speed set to {speed} ({SPEED_RATES[speed]} wpm)")

    @property
    def current_speed(self) -> str:
        return self._speed

    # ------------------------------------------------------------------
    # Public: TTS
    # ------------------------------------------------------------------

    def speak_text(
        self,
        text: str,
        language: Optional[str] = None,
        speed: Optional[str] = None,
    ) -> None:
        if not text or not text.strip():
            return

        lang = language or self._language
        rate_key = speed or self._speed
        rate = SPEED_RATES.get(rate_key, SPEED_RATES["medium"])
        self._last_spoken = text

        detected = detect_language(text)

        if detected == "mixed":
            self._speak_mixed(text, rate, rate_key)
        elif detected == "tamil" or lang == "ta":
            self._speak_tamil(text, rate_key)
        else:
            self._pyttsx3.speak(text, rate)

    def speak(self, text: str) -> None:
        self.speak_text(text)

    def repeat_last(self) -> None:
        if self._last_spoken:
            self.speak_text(self._last_spoken)
        else:
            self.speak_text("Nothing to repeat.")

    def _speak_tamil(self, text: str, speed_key: str) -> None:
        slow = speed_key == "slow"
        success = self._gtts.speak(text, slow=slow)
        if not success:
            logger.warning("gTTS unavailable; falling back to pyttsx3 for Tamil text")
            self._pyttsx3.speak(text, SPEED_RATES[speed_key])

    def _speak_mixed(self, text: str, rate: int, speed_key: str) -> None:
        segments = self._split_mixed_content(text)
        for segment_text, segment_lang in segments:
            if not segment_text.strip():
                continue
            if segment_lang == "tamil":
                self._speak_tamil(segment_text, speed_key)
            else:
                self._pyttsx3.speak(segment_text, rate)

    def _split_mixed_content(self, text: str) -> List[Tuple[str, str]]:
        segments: List[Tuple[str, str]] = []
        current_chars: List[str] = []
        current_lang: Optional[str] = None

        for ch in text:
            ch_lang = "tamil" if _TAMIL_START <= ord(ch) <= _TAMIL_END else "english"
            if current_lang is None:
                current_lang = ch_lang

            if ch_lang != current_lang and ch.isalpha():
                if current_chars:
                    segments.append(("".join(current_chars), current_lang))
                current_chars = [ch]
                current_lang = ch_lang
            else:
                current_chars.append(ch)

        if current_chars and current_lang:
            segments.append(("".join(current_chars), current_lang))

        return segments

    # ------------------------------------------------------------------
    # Public: STT
    # ------------------------------------------------------------------

    def listen_to_command(self, language: Optional[str] = None) -> Optional[str]:
        lang = language or self._language
        lang_code = (
            settings.voice.stt_tamil_language if lang == "ta"
            else settings.voice.stt_language
        )

        audio = self._capture_audio()
        if audio is None:
            return None

        text = self._transcribe_with_fallback(audio, lang_code, lang)
        if text:
            logger.info(f"Transcribed: '{text}'")
            return text.strip()
        return None

    def listen(self) -> Optional[str]:
        return self.listen_to_command()

    def _capture_audio(self) -> Optional[sr.AudioData]:
        try:
            with sr.Microphone() as source:
                logger.debug("Calibrating for ambient noise…")
                self._recognizer.adjust_for_ambient_noise(
                    source,
                    duration=settings.voice.noise_calibration_duration,
                )
                logger.info("Listening…")
                audio = self._recognizer.listen(
                    source,
                    timeout=settings.voice.timeout,
                    phrase_time_limit=settings.voice.phrase_time_limit,
                )
            return audio
        except sr.WaitTimeoutError:
            logger.info("Listen timeout — no speech detected")
            return None
        except Exception as e:
            logger.error(f"Microphone capture error: {e}")
            return None

    def _transcribe_with_fallback(
        self, audio: sr.AudioData, lang_code: str, lang: str
    ) -> Optional[str]:
        try:
            text = self._google_stt.transcribe(audio, lang_code)
            if text:
                return text
        except Exception:
            logger.info("Google STT failed (offline?); trying Vosk…")
            text = self._vosk_stt.transcribe(audio, lang)
            if text:
                return text

        logger.warning("Both STT engines failed to transcribe")
        return None

    # ------------------------------------------------------------------
    # Public: command matching
    # ------------------------------------------------------------------

    def match_command(self, text: str) -> Optional[str]:
        if not text:
            return None
        lowered = text.lower().strip()
        for command, phrases in COMMAND_VOCAB.items():
            for phrase in phrases:
                if phrase.lower() in lowered:
                    return command
        return None

    # ------------------------------------------------------------------
    # Public: voice navigation
    # ------------------------------------------------------------------

    def voice_menu(
        self,
        options: List[str],
        header: str = "Please choose an option.",
    ) -> Optional[int]:
        max_retries = settings.voice.menu_max_retries

        for attempt in range(1, max_retries + 1):
            self.speak_text(header)
            for i, option in enumerate(options, start=1):
                self.speak_text(f"{i}. {option}")
                time.sleep(0.15)
            self.speak_text("Say the number of your choice.")

            response = self.listen_to_command()
            if response is None:
                self.speak_text("I did not hear anything. Please try again.")
                continue

            selection = self._parse_number(response)
            if selection is not None and 1 <= selection <= len(options):
                self.speak_text(f"You selected: {options[selection - 1]}")
                return selection

            if attempt < max_retries:
                self.speak_text(
                    f"Sorry, I did not understand '{response}'. "
                    f"Please say a number between 1 and {len(options)}."
                )
            else:
                self.speak_text("Maximum retries reached. Returning to menu.")

        return None

    def confirm_action(self, action: str) -> bool:
        max_retries = settings.voice.confirm_max_retries
        prompt = f"Are you sure you want to {action}? Say yes or no."

        for attempt in range(1, max_retries + 1):
            self.speak_text(prompt)
            response = self.listen_to_command()

            if response is None:
                if attempt < max_retries:
                    self.speak_text("I did not hear you. Please say yes or no.")
                continue

            command = self.match_command(response)
            if command == "yes":
                self.speak_text("Confirmed.")
                return True
            if command == "no":
                self.speak_text("Cancelled.")
                return False

            if attempt < max_retries:
                self.speak_text(f"Please say yes or no. You said: {response}.")

        self.speak_text("No confirmation received. Action cancelled.")
        return False

    # ------------------------------------------------------------------
    # Public: accessibility helpers
    # ------------------------------------------------------------------

    def announce(self, message: str) -> None:
        logger.info(f"[AUDIO FEEDBACK] {message}")
        self.speak_text(message)

    def play_earcon(self, event: str) -> None:
        earcons = {
            "success":  "Done.",
            "error":    "Error.",
            "start":    "Ready.",
            "timeout":  "Time out.",
            "bookmark": "Bookmarked.",
            "loading":  "Loading. Please wait.",
        }
        phrase = earcons.get(event, event)
        self._pyttsx3.speak(phrase, SPEED_RATES["fast"])

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_number(self, text: str) -> Optional[int]:
        lowered = text.lower().strip()

        digits = re.findall(r"\b(\d+)\b", lowered)
        if digits:
            return int(digits[0])

        for word, num in NUMBER_WORDS.items():
            if word in lowered:
                return num

        return None


# ---------------------------------------------------------------------------
# Module-level standalone functions (public API)
# ---------------------------------------------------------------------------

_default_module: Optional[VoiceModule] = None


def _get_module() -> VoiceModule:
    global _default_module
    if _default_module is None:
        _default_module = VoiceModule()
    return _default_module


def listen_to_command(language: Optional[str] = None) -> Optional[str]:
    return _get_module().listen_to_command(language=language)


def speak_text(
    text: str,
    language: Optional[str] = None,
    speed: Optional[str] = None,
) -> None:
    _get_module().speak_text(text, language=language, speed=speed)


def voice_menu(
    options: List[str],
    header: str = "Please choose an option.",
) -> Optional[int]:
    return _get_module().voice_menu(options, header=header)


def confirm_action(action: str) -> bool:
    return _get_module().confirm_action(action)
