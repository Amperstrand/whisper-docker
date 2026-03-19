# NVIDIA CUDA 12 + cuDNN 9 runtime — matches faster-whisper / CTranslate2 GPU requirements.
# Driver 580.x (and most recent drivers) are forward-compatible with CUDA 12.6 images.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3, pip, and ffmpeg (required by faster-whisper for audio decoding).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install faster-whisper with all dependencies.
# requests is needed for model download; huggingface-hub for model caching.
# pyannote.audio + torchaudio are for optional speaker diarization (GPU-accelerated).
RUN pip3 install --no-cache-dir --break-system-packages \
    requests \
    huggingface_hub \
    faster-whisper==1.2.1 \
    torchaudio==2.6.0 \
    pyannote.audio==3.3.2

# Use existing 'ubuntu' user from base image (uid 1000).
# Copy the transcription script with proper ownership.
COPY --chown=ubuntu:ubuntu transcribe.py /app/transcribe.py

WORKDIR /app

# Run as non-root user for security.
USER ubuntu

ENTRYPOINT ["python3", "transcribe.py"]
