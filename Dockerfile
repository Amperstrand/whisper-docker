# NVIDIA CUDA 12 + cuDNN 9 runtime — matches faster-whisper / CTranslate2 GPU requirements.
# Driver 580.x (and most recent drivers) are forward-compatible with CUDA 12.6 images.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

# Standardize cache paths so all model downloads land in the mounted host cache.
ENV TORCH_HOME=/home/ubuntu/.cache/torch
ENV HF_HOME=/home/ubuntu/.cache/huggingface

# Install Python 3, pip, and ffmpeg (required by faster-whisper for audio decoding).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Core transcription + optional analysis pipeline dependencies.
# huggingface_hub pinned for pyannote compatibility.
RUN pip3 install --no-cache-dir --break-system-packages \
    requests \
    "huggingface_hub==0.23.5" \
    faster-whisper==1.2.1 \
    torchaudio==2.6.0 \
    pyannote.audio==3.3.2 \
    matplotlib \
    speechbrain \
    transformers \
    timm

# Use existing 'ubuntu' user from base image (uid 1000).
COPY --chown=ubuntu:ubuntu transcribe.py /app/transcribe.py

WORKDIR /app

USER ubuntu

ENTRYPOINT ["python3", "transcribe.py"]
