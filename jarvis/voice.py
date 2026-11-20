"""Ears and mouth: microphone -> text (wake word) and text -> speech.

Speech recognition uses the free Google web recogniser through the
SpeechRecognition package (needs internet, no key). Speech output uses the
offline Windows voices through pyttsx3.
"""
from __future__ import annotations

import logging
import re
import threading
import time

log = logging.getLogger("jarvis.voice")

YES_WORDS = ("yes", "yeah", "yep", "yup", "sure", "proceed", "do it", "confirm", "go ahead", "okay", "ok")


def strip_wake(text: str, wake_words: list) -> str | None:
    """Return what follows the wake word, or None if the wake word wasn't said."""
    low = text.lower()
    for word in wake_words:
        m = re.search(rf"\b{re.escape(word)}\b", low)
        if m:
            return text[m.end():].strip(" ,.!?:;-")
    return None


def clean_for_speech(text: str) -> str:
    text = re.sub(r"https?://\S+", "the link", text)
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


class Voice:
    def __init__(self, settings):
        import speech_recognition as sr  # raises ImportError if missing -> main falls back to text mode

        self.sr = sr
        self.rate = settings.voice_rate
        self.rec = sr.Recognizer()
        self.rec.dynamic_energy_threshold = True
        self.rec.pause_threshold = 0.8
        self.mic = sr.Microphone()
        with self.mic as source:
            self.rec.adjust_for_ambient_noise(source, duration=1)
        self._speak_lock = threading.Lock()

    def speak(self, text: str) -> None:
        text = clean_for_speech(text)
        if not text:
            return
        print(f"jarvis> {text}")
        with self._speak_lock:
            try:
                import pyttsx3

                engine = pyttsx3.init()  # fresh engine each time avoids a known runAndWait hang on Windows
                engine.setProperty("rate", self.rate)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
            except Exception:
                log.exception("text-to-speech failed")

    def listen(self, timeout: float = 5, phrase_limit: float = 15) -> str | None:
        with self.mic as source:
            try:
                audio = self.rec.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
            except self.sr.WaitTimeoutError:
                return None
        try:
            text = self.rec.recognize_google(audio)
        except self.sr.UnknownValueError:
            return None
        except self.sr.RequestError as e:
            log.warning("speech recognition unavailable: %s", e)
            time.sleep(2)
            return None
        log.info("heard: %s", text)
        return text

    def confirm(self, question: str) -> bool:
        self.speak(question)
        answer = self.listen(timeout=7, phrase_limit=5)
        return bool(answer) and any(w in answer.lower() for w in YES_WORDS)
