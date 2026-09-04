
"""
voice_agent.py
--------------
Defines `VoiceAgent`: converts a final outbound message string into a
locally-synthesized voice (.wav) file, using a standalone, pre-compiled
Piper executable bundled directly in this repository under `bin/piper/`.

Why the standalone executable (bypassing `pip install piper-tts` entirely):
    `piper-tts`'s pip dependency, `piper-phonemize`, was never published as
    a Windows wheel and its upstream repo is now archived, making
    `pip install` fundamentally broken on Windows regardless of Python
    version. The official Piper GitHub releases publish a fully
    self-contained Windows binary (`piper.exe` + bundled DLLs +
    `espeak-ng-data/`) that needs no Python packaging at all -- this
    module shells out to that binary directly, the same way the original
    Piper-based design always intended `VoiceAgent` to work.

Why the local `hi_IN-rohan-medium` model:
    This gives us a real, native Hindi voice (MIT-licensed, from the
    official `rhasspy/piper-voices` catalog) rather than relying on
    English phonemization of Hinglish text. See README.md for the
    download source and setup instructions.

Security note (command injection) -- unchanged from the original design:
    Text passed to `piper.exe` is customer/LLM-derived. Rather than
    building a shell string, this module invokes `piper.exe` as a
    list-form subprocess argv (`shell=False`, the `subprocess.run`
    default) and feeds text via `stdin` using `input=`. No shell ever
    parses the message text, so shell-metacharacter injection is not a
    viable attack surface. `customer_id` is separately reduced to an
    alphanumeric/hyphen/underscore token before being used to build an
    output file path, preventing path traversal.

Fallback behavior -- unchanged from the original design:
    If the local executable or model file is missing, or if `piper.exe`
    fails/times out for any reason, `generate_voice_message()` logs a
    warning and returns None rather than raising. A customer-facing
    message must never depend on voice synthesis succeeding.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

# Project root, resolved relative to this file (messaging/voice_agent.py
# -> repo root is one level up), so this works regardless of the working
# directory the Flask app or a script is launched from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PIPER_DIR = PROJECT_ROOT / "bin" / "piper"
DEFAULT_PIPER_EXECUTABLE = DEFAULT_PIPER_DIR / "piper.exe"
DEFAULT_PIPER_MODEL = PROJECT_ROOT / "models" / "hi_IN-priyamvada-medium.onnx"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "audio" / "generated"
DEFAULT_TIMEOUT_SECONDS = 30


class VoiceAgent:
    """
    Synthesizes a customer-facing message into speech using a local,
    standalone Piper executable and a local Hindi voice model -- no pip
    package, no PATH dependency, no global install required.

    Usage:
        agent = VoiceAgent()
        filepath = agent.generate_voice_message(
            text="Hi Rohan, aapka payment fail ho gaya hai...",
            customer_id="CUST-000042",
        )
        # filepath is a relative path string, or None if synthesis was
        # unavailable/failed for any reason.
    """

    def __init__(
        self,
        piper_executable: Path = DEFAULT_PIPER_EXECUTABLE,
        model_path: Path = DEFAULT_PIPER_MODEL,
        output_root: Path = DEFAULT_OUTPUT_ROOT,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.piper_executable = Path(piper_executable)
        self.model_path = Path(model_path)
        self.output_root = Path(output_root)
        self.timeout_seconds = timeout_seconds

        # Fail-safe checks happen once at construction, not on every call,
        # so a broken setup is visible immediately (e.g. at Flask startup)
        # rather than silently on the first customer message.
        self._executable_available = self._check_executable()
        self._model_available = self._check_model()

    def _check_executable(self) -> bool:
        if not self.piper_executable.exists():
            print(
                f"[VoiceAgent] WARNING: piper.exe not found at "
                f"{self.piper_executable}. Download it from "
                "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip "
                "and extract into bin/piper/. Voice synthesis is unavailable."
            )
            return False
        return True

    def _check_model(self) -> bool:
        onnx_json = Path(str(self.model_path) + ".json")
        if not self.model_path.exists() or not onnx_json.exists():
            print(
                f"[VoiceAgent] WARNING: Voice model not found at "
                f"{self.model_path} (and its matching .onnx.json). "
                "Download hi_IN-priyamvada-medium.onnx and hi_IN-priyamvada-medium.onnx.json "
                "from https://huggingface.co/rhasspy/piper-voices/tree/main/hi/hi_IN/priyamvada/medium "
                "into models/. Voice synthesis is unavailable."
            )
            return False
        return True

    def generate_voice_message(self, text: str, customer_id: str) -> Optional[str]:
        """
        Generates a .wav voice file for `text` and returns its relative
        filepath, or None if synthesis was unavailable/failed.

        Args:
            text: The message text to speak (plain text).
            customer_id: Used to build a predictable, per-customer output
                filename. Sanitized to a safe filesystem token before use
                to prevent path traversal via a malformed customer_id.

        Returns:
            Relative filepath string on success, or None if the local
            executable/model is missing, piper.exe times out, exits
            non-zero, or any other error occurs.
        """
        if not self._executable_available or not self._model_available:
            print("[VoiceAgent] WARNING: Setup incomplete; skipping synthesis.")
            return None

        if not text or not text.strip():
            print("[VoiceAgent] WARNING: Empty text supplied; skipping synthesis.")
            return None

        safe_customer_id = self._sanitize_customer_id(customer_id)
        output_path = self.output_root / f"{safe_customer_id}_message.wav"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            result = subprocess.run(
                [
                    str(self.piper_executable),
                    "--model",
                    str(self.model_path),
                    "--output_file",
                    str(output_path),
                ],
                input=text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_seconds,
                check=False,
                # Run with cwd set to Piper's own folder so it reliably
                # finds its bundled espeak-ng-data/ directory via a
                # relative path, regardless of where this Python process
                # itself was launched from.
                cwd=str(DEFAULT_PIPER_DIR),
            )
        except FileNotFoundError:
            print(
                f"[VoiceAgent] WARNING: Could not execute {self.piper_executable}. "
                "Skipping voice synthesis."
            )
            return None
        except subprocess.TimeoutExpired:
            print(
                f"[VoiceAgent] WARNING: piper.exe synthesis timed out after "
                f"{self.timeout_seconds}s for customer_id={safe_customer_id!r}."
            )
            return None
        except Exception as exc:  # noqa: BLE001 - final safety net
            print(
                f"[VoiceAgent] WARNING: Unexpected error invoking piper.exe "
                f"({exc}). Skipping voice synthesis."
            )
            return None

        if result.returncode != 0:
            print(
                f"[VoiceAgent] WARNING: piper.exe exited with code "
                f"{result.returncode} for customer_id={safe_customer_id!r}. "
                f"stderr: {result.stderr.strip()}"
            )
            return None

        if not output_path.exists():
            print(
                f"[VoiceAgent] WARNING: piper.exe reported success but no "
                f"output file was found at {output_path}."
            )
            return None

        return output_path.name

    @staticmethod
    def _sanitize_customer_id(customer_id: str) -> str:
        """
        Reduces `customer_id` to a filesystem-safe token (alphanumeric,
        hyphen, underscore only) before it is used to build a file path.
        This prevents path traversal (e.g. "../../etc/passwd") or invalid
        filename characters from a malformed/malicious caller.
        """
        safe = "".join(
            ch for ch in str(customer_id) if ch.isalnum() or ch in ("-", "_")
        ).strip("._")
        return safe or "unknown_customer"
