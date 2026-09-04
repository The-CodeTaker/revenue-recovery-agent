"""
test_piper_manual.py
----------------------
Standalone end-to-end smoke test for the local, standalone-executable
Piper voice pipeline. Run directly (not via pytest) to confirm the
bin/piper/piper.exe + models/hi_IN-rohan-medium.onnx setup actually works.

Usage:
    python test_piper_manual.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MINIMUM_EXPECTED_WAV_BYTES = 5_000  # a real utterance is never this small


def main() -> None:
    from messaging.voice_agent import VoiceAgent

    print("=" * 60)
    print("STEP 1: Initializing VoiceAgent (standalone piper.exe)")
    print("=" * 60)
    agent = VoiceAgent()

    if not agent._executable_available:
        print("\n🛑 piper.exe not found. See the warning above for the download URL.")
        sys.exit(1)

    if not agent._model_available:
        print("\n🛑 Voice model not found. See the warning above for the download URLs.")
        sys.exit(1)

    print("✅ Executable and model both found.")

    print("\n" + "=" * 60)
    print("STEP 2: Generating a Hinglish test message")
    print("=" * 60)

    sample_text = (
        "Hi Rohan, aapka Pro Monthly ka payment of 1,499 rupees fail ho "
        "gaya hai. Kripya apna payment method update kar lijiye."
    )

    filepath = agent.generate_voice_message(text=sample_text, customer_id="CUST-TEST-PIPER")

    if filepath is None:
        print("❌ generate_voice_message() returned None. See warnings above for the cause.")
        sys.exit(1)

    full_path = Path(filepath)
    if not full_path.exists():
        print(f"❌ Reported success but file does not exist at {full_path}.")
        sys.exit(1)

    size_bytes = full_path.stat().st_size
    print(f"✅ Generated: {full_path} ({size_bytes:,} bytes)")

    print("\n" + "=" * 60)
    print("STEP 3: Verifying file is non-trivial")
    print("=" * 60)

    if size_bytes == 0:
        print("❌ File is zero bytes -- synthesis produced no audio.")
        sys.exit(1)
    elif size_bytes < MINIMUM_EXPECTED_WAV_BYTES:
        print(
            f"⚠️  File is smaller than expected (< {MINIMUM_EXPECTED_WAV_BYTES:,} bytes) "
            "-- check the output manually before trusting this in a demo."
        )
    else:
        print(f"✅ File size ({size_bytes:,} bytes) is within expected range.")

    print("\n" + "=" * 60)
    print(f"SUMMARY: ✅ PASSED -- {full_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()