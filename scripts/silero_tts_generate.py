#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import torch


SAMPLE_RATE = 48_000


def write_wav(path: Path, audio: np.ndarray) -> None:
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="v4_ru")
    parser.add_argument("--speaker", default="aidar")
    args = parser.parse_args()

    torch.set_num_threads(2)
    device = torch.device("cpu")
    model, _example_text = torch.hub.load(
        repo_or_dir="snakers4/silero-models",
        model="silero_tts",
        language="ru",
        speaker=args.model,
        trust_repo=True,
    )
    model.to(device)
    audio = model.apply_tts(
        text=args.text,
        speaker=args.speaker,
        sample_rate=SAMPLE_RATE,
        put_accent=True,
        put_yo=True,
    )
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    else:
        audio = np.asarray(audio)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav_file:
        write_wav(Path(wav_file.name), audio.astype(np.float32))
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                wav_file.name,
                "-ac",
                "1",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                str(output_path),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
