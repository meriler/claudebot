"""Voice transcription fallback chain: Deepgram → fluidaudiocli (Parakeet TDT v3) → Yandex STT."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from typing import Any

import aiohttp
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from deepgram import AsyncDeepgramClient

from telegram_bot.core.config import Settings

logger = logging.getLogger(__name__)

_DEEPGRAM_TIMEOUT_SEC = 30
_FLUIDAUDIO_TIMEOUT_SEC = 120
_YANDEX_TIMEOUT_SEC = 30
_YANDEX_MAX_DURATION_SEC = 30
_IAM_TOKEN_TTL_SEC = 3600 * 10
_IAM_TOKEN_REFRESH_MARGIN_SEC = 300
_IAM_TOKEN_ENDPOINT = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
_YANDEX_STT_ENDPOINT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"


class TranscriptionError(Exception):
    """Raised when transcription fails."""


class Transcriber:
    def __init__(self, settings: Settings) -> None:
        self._deepgram_enabled = bool(settings.deepgram_api_key)
        self._deepgram_client = (
            AsyncDeepgramClient(api_key=settings.deepgram_api_key)
            if self._deepgram_enabled
            else None
        )

        self._fluidaudio_cli = settings.fluidaudio_cli_path
        self._fluidaudio_model_dir = settings.fluidaudio_model_dir
        self._fluidaudio_enabled = bool(self._fluidaudio_cli and self._fluidaudio_model_dir)
        if self._fluidaudio_enabled and not os.path.isfile(self._fluidaudio_cli):
            logger.warning("fluidaudiocli not found at %s, fallback disabled", self._fluidaudio_cli)
            self._fluidaudio_enabled = False
        if self._fluidaudio_enabled and not os.path.isdir(self._fluidaudio_model_dir):
            logger.warning(
                "FluidAudio model dir not found at %s, fallback disabled",
                self._fluidaudio_model_dir,
            )
            self._fluidaudio_enabled = False
        self._fluidaudio_semaphore = asyncio.Semaphore(1)

        self._yandex_folder_id = settings.yandex_folder_id
        self._yandex_sa_key: dict[str, Any] | None = None
        self._yandex_private_key: RSAPrivateKey | None = None
        self._yandex_enabled = False
        self._iam_cache: tuple[str, float] | None = None

        if settings.yandex_sa_key_json:
            try:
                sa = json.loads(settings.yandex_sa_key_json)
                pk_raw = load_pem_private_key(sa["private_key"].encode(), password=None)
                if not isinstance(pk_raw, RSAPrivateKey):
                    raise TypeError("Yandex SA key must be RSA")
                pk = pk_raw
                self._yandex_sa_key = sa
                self._yandex_private_key = pk
                self._yandex_enabled = bool(settings.yandex_folder_id)
            except Exception:
                logger.warning(
                    "Failed to parse Yandex SA key, Yandex fallback disabled",
                    exc_info=True,
                )

        backends = []
        if self._deepgram_enabled:
            backends.append("Deepgram")
        if self._fluidaudio_enabled:
            backends.append("fluidaudiocli")
        if self._yandex_enabled:
            backends.append("Yandex")
        logger.info("Transcription backends: %s", ", ".join(backends) or "none")

    @property
    def _enabled(self) -> bool:
        return self._deepgram_enabled or self._fluidaudio_enabled or self._yandex_enabled

    async def transcribe(self, audio_data: bytes) -> str:
        """Transcribe audio bytes. Tries backends in order until one succeeds."""
        if not self._enabled:
            raise TranscriptionError("No transcription backend configured")

        last_error: TranscriptionError | None = None

        if self._deepgram_enabled:
            try:
                return await self._transcribe_deepgram(audio_data)
            except TranscriptionError as exc:
                last_error = exc
                logger.warning("Deepgram failed, trying fallbacks: %s", exc)

        if self._fluidaudio_enabled:
            try:
                return await self._transcribe_fluidaudio(audio_data)
            except TranscriptionError as exc:
                last_error = exc
                logger.warning("fluidaudiocli failed, trying next fallback: %s", exc)

        if self._yandex_enabled:
            try:
                return await self._transcribe_yandex(audio_data)
            except TranscriptionError as exc:
                last_error = exc
                logger.warning("Yandex failed: %s", exc)

        raise last_error or TranscriptionError("All transcription backends failed")

    # -- Deepgram ---------------------------------------------------------

    async def _ogg_to_wav(self, audio_data: bytes) -> bytes:
        """Перекодировать вход (OGG/Opus от Telegram) в WAV 16k mono через ffmpeg.

        Голосовые Telegram часто несут битый/нулевой заголовок длительности в
        OGG-контейнере; Deepgram по нему обрывает расшифровку на первых секундах
        (возвращает HTTP 200 + частичный текст, поэтому фолбэк не срабатывает).
        Прогон через ffmpeg перезаписывает контейнер с корректной длительностью.
        При любом сбое возвращаем исходные байты — путь не ломаем.
        """
        ogg_path = wav_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                f.write(audio_data)
                ogg_path = f.name
            wav_path = ogg_path[:-4] + ".wav"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                ogg_path,
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "wav",
                wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await asyncio.wait_for(proc.wait(), timeout=30)
            if rc != 0:
                return audio_data
            with open(wav_path, "rb") as f:
                return f.read()
        except Exception:
            return audio_data
        finally:
            for p in (ogg_path, wav_path):
                if p and os.path.exists(p):
                    with contextlib.suppress(OSError):
                        os.unlink(p)

    async def _transcribe_deepgram(self, audio_data: bytes) -> str:
        if not self._deepgram_client:
            raise TranscriptionError("Deepgram not configured")
        audio_data = await self._ogg_to_wav(audio_data)
        try:
            response = await asyncio.wait_for(
                self._deepgram_client.listen.v1.media.transcribe_file(
                    request=audio_data,
                    model="nova-3",
                    language="multi",
                    smart_format=True,
                ),
                timeout=_DEEPGRAM_TIMEOUT_SEC,
            )
        except TimeoutError:
            raise TranscriptionError("Deepgram transcription timed out") from None
        except Exception as exc:
            raise TranscriptionError(f"Deepgram API error: {exc}") from exc

        channels = response.results.channels
        if not channels or not channels[0].alternatives:
            return ""
        transcript: str = channels[0].alternatives[0].transcript
        logger.info("Transcription done (Deepgram), length=%d chars", len(transcript))
        return transcript

    # -- fluidaudiocli (Parakeet TDT v3) -----------------------------------

    async def _transcribe_fluidaudio(self, audio_data: bytes) -> str:
        async with self._fluidaudio_semaphore:
            return await self._run_fluidaudio(audio_data)

    async def _run_fluidaudio(self, audio_data: bytes) -> str:
        ogg_path = ""
        wav_path = ""
        ffmpeg_proc: asyncio.subprocess.Process | None = None
        fluidaudio_proc: asyncio.subprocess.Process | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                f.write(audio_data)
                ogg_path = f.name
            wav_path = ogg_path.replace(".ogg", ".wav")

            ffmpeg_proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                ogg_path,
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "wav",
                wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                rc = await asyncio.wait_for(ffmpeg_proc.wait(), timeout=30)
            except TimeoutError:
                ffmpeg_proc.kill()
                await ffmpeg_proc.wait()
                raise TranscriptionError("ffmpeg timed out") from None
            if rc != 0:
                raise TranscriptionError(f"ffmpeg exited with code {rc}")
            ffmpeg_proc = None

            fluidaudio_proc = await asyncio.create_subprocess_exec(
                self._fluidaudio_cli,
                "transcribe",
                wav_path,
                "--model-dir",
                self._fluidaudio_model_dir,
                "--language",
                "ru",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    fluidaudio_proc.communicate(),
                    timeout=_FLUIDAUDIO_TIMEOUT_SEC,
                )
            except TimeoutError:
                fluidaudio_proc.kill()
                await fluidaudio_proc.wait()
                raise TranscriptionError("fluidaudiocli timed out") from None
            if fluidaudio_proc.returncode != 0:
                raise TranscriptionError(
                    f"fluidaudiocli exited with code {fluidaudio_proc.returncode}"
                )
            fluidaudio_proc = None

            transcript = stdout.decode("utf-8", errors="replace").strip()
            logger.info("Transcription done (fluidaudiocli), length=%d chars", len(transcript))
            return transcript

        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"fluidaudiocli error: {exc}") from exc
        finally:
            for proc in (ffmpeg_proc, fluidaudio_proc):
                if proc and proc.returncode is None:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await proc.wait()
            for p in (ogg_path, wav_path):
                if p:
                    with contextlib.suppress(OSError):
                        os.unlink(p)

    # -- Yandex SpeechKit REST -------------------------------------------

    async def _transcribe_yandex(self, audio_data: bytes) -> str:
        duration = await self._probe_duration(audio_data)
        if duration is not None and duration > _YANDEX_MAX_DURATION_SEC:
            raise TranscriptionError(
                f"Audio too long for Yandex REST ({duration:.0f}s > {_YANDEX_MAX_DURATION_SEC}s)"
            )

        iam_token = await self._get_iam_token()

        try:
            async with aiohttp.ClientSession() as session:
                resp = await asyncio.wait_for(
                    session.post(
                        _YANDEX_STT_ENDPOINT,
                        params={
                            "folderId": self._yandex_folder_id,
                            "lang": "ru-RU",
                            "format": "oggopus",
                        },
                        headers={"Authorization": f"Bearer {iam_token}"},
                        data=audio_data,
                    ),
                    timeout=_YANDEX_TIMEOUT_SEC,
                )
                if resp.status != 200:
                    body = await resp.text()
                    raise TranscriptionError(f"Yandex STT HTTP {resp.status}: {body[:200]}")
                data = await resp.json()
        except TranscriptionError:
            raise
        except TimeoutError:
            raise TranscriptionError("Yandex STT timed out") from None
        except Exception as exc:
            raise TranscriptionError(f"Yandex STT error: {exc}") from exc

        transcript: str = data.get("result", "")
        logger.info("Transcription done (Yandex), length=%d chars", len(transcript))
        return transcript

    async def _get_iam_token(self) -> str:
        now = time.time()
        if self._iam_cache and self._iam_cache[1] > now + _IAM_TOKEN_REFRESH_MARGIN_SEC:
            return self._iam_cache[0]

        if not self._yandex_sa_key or not self._yandex_private_key:
            raise TranscriptionError("Yandex SA key not loaded")

        jwt_payload = {
            "aud": _IAM_TOKEN_ENDPOINT,
            "iss": self._yandex_sa_key["service_account_id"],
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        token = jwt.encode(
            jwt_payload,
            self._yandex_private_key,
            algorithm="PS256",
            headers={"kid": self._yandex_sa_key["id"]},
        )

        try:
            async with aiohttp.ClientSession() as session:
                resp = await asyncio.wait_for(
                    session.post(
                        _IAM_TOKEN_ENDPOINT,
                        json={"jwt": token},
                    ),
                    timeout=10,
                )
                resp.raise_for_status()
                data = await resp.json()
        except Exception as exc:
            raise TranscriptionError(f"IAM token request failed: {exc}") from exc

        iam_token: str = data.get("iamToken", "")
        if not iam_token:
            raise TranscriptionError("IAM token empty in response")

        self._iam_cache = (iam_token, now + _IAM_TOKEN_TTL_SEC)
        logger.info("Yandex IAM token refreshed")
        return iam_token

    async def _probe_duration(self, audio_data: bytes) -> float | None:
        """Get audio duration via ffprobe. Returns None if probe fails."""
        ogg_path = ""
        proc: asyncio.subprocess.Process | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                f.write(audio_data)
                ogg_path = f.name

            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                ogg_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return None
            return float(stdout.decode().strip())
        except Exception:
            return None
        finally:
            if proc and proc.returncode is None:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            if ogg_path:
                with contextlib.suppress(OSError):
                    os.unlink(ogg_path)
