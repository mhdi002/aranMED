# Copyright (c) 2024–2026 aranmed — Triton Python backend for Whisper ASR.
# Loads openai/whisper-large-v3 (or WHISPER_MODEL_DIR) and serves
# WAV (FP32, 16 kHz mono) → TRANSCRIPT (UTF-8 string).
#
# Long audio (> WHISPER_MAX_SHORTFORM_S): use pipeline chunk_length_s /
# stride_length_s so the full file is transcribed end-to-end. Whisper's
# internal long-form seek alone often stops mid-file on mixed FA/EN dictation.

import json
import os
from typing import Optional

import numpy as np
import triton_python_backend_utils as pb_utils


class TritonPythonModel:
    def initialize(self, args):
        self.model_config = json.loads(args["model_config"])
        model_dir = (
            os.environ.get("WHISPER_MODEL_DIR")
            or os.environ.get("ASR_WHISPER_PATH")
            or ""
        ).strip()
        if not model_dir:
            model_dir = "openai/whisper-large-v3"

        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        self.target_sr = int(os.environ.get("WHISPER_TARGET_SR", "16000"))
        self.max_shortform_s = float(os.environ.get("WHISPER_MAX_SHORTFORM_S", "30"))
        self.chunk_length_s = float(os.environ.get("WHISPER_CHUNK_LENGTH_S", "30"))
        self.stride_length_s = float(os.environ.get("WHISPER_STRIDE_LENGTH_S", "0"))

        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_dir,
            torch_dtype=dtype,
            low_cpu_mem_usage=True,
            use_safetensors=True,
        )
        model.to(self.device)
        if getattr(model, "generation_config", None) is not None:
            model.generation_config.forced_decoder_ids = None
        processor = AutoProcessor.from_pretrained(model_dir)
        self.pipe = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            torch_dtype=dtype,
            device=0 if self.device.startswith("cuda") else -1,
        )
        # Match hf_asr output_english=true (English for LLM / report paths).
        output_english = os.environ.get("WHISPER_OUTPUT_ENGLISH", "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        self._task = (
            "translate"
            if output_english
            else os.environ.get("WHISPER_TASK", "transcribe")
        )

    def _transcribe_full(self, audio: np.ndarray, language: Optional[str]) -> str:
        gen_kwargs = {"task": self._task}
        if language:
            gen_kwargs["language"] = language

        duration_s = float(audio.size) / float(self.target_sr)
        if duration_s <= self.max_shortform_s:
            result = self.pipe(
                {"array": audio, "sampling_rate": self.target_sr},
                generate_kwargs=gen_kwargs,
                return_timestamps=True,
            )
            text = (result.get("text") if isinstance(result, dict) else str(result)) or ""
            return text.strip()

        chunk_n = max(1, int(round(self.chunk_length_s * self.target_sr)))
        overlap_n = max(0, int(round(self.stride_length_s * self.target_sr)))
        hop = max(1, chunk_n - overlap_n)
        parts = []
        start = 0
        total = int(audio.size)
        while start < total:
            end = min(start + chunk_n, total)
            piece = audio[start:end]
            if piece.size < int(0.5 * self.target_sr):
                break
            result = self.pipe(
                {"array": piece, "sampling_rate": self.target_sr},
                generate_kwargs=gen_kwargs,
                return_timestamps=True,
            )
            piece_text = (result.get("text") if isinstance(result, dict) else str(result)) or ""
            piece_text = piece_text.strip()
            if piece_text:
                parts.append(piece_text)
            if end >= total:
                break
            start += hop
        return " ".join(parts).strip()

    def execute(self, requests):
        responses = []
        for request in requests:
            wav_t = pb_utils.get_input_tensor_by_name(request, "WAV")
            audio = wav_t.as_numpy().astype(np.float32).reshape(-1)

            language: Optional[str] = None
            lang_t = pb_utils.get_input_tensor_by_name(request, "LANGUAGE")
            if lang_t is not None:
                raw = lang_t.as_numpy().reshape(-1)[0]
                if isinstance(raw, (bytes, bytearray)):
                    language = raw.decode("utf-8")
                else:
                    language = str(raw)
                if not language or language.lower() in ("none", "null", ""):
                    language = None

            text = self._transcribe_full(audio, language)

            out = pb_utils.Tensor(
                "TRANSCRIPT",
                np.array([text.encode("utf-8")], dtype=object),
            )
            responses.append(pb_utils.InferenceResponse(output_tensors=[out]))
        return responses

    def finalize(self):
        self.pipe = None
