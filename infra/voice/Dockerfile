# Logos voice service: speech-to-text for every agent, in one place (F31).
#
# One container holds one Whisper model, so the server pays for it once instead of once per
# agent, and the agent image stays upstream. Agents send a voice note here and get text back
# (gateway/voice.py, mounted read-only at start; gateway/voice_host.py runs it).
#
# Third-party parts, approved by Greg on 2026-11-16:
#   faster-whisper 1.2.1 and av 18.1.0  the same pins hermes-agent v0.23.1 uses for its own
#                                       speech-to-text extra
#   Systran/faster-whisper-small        multilingual Whisper "small" (English and French),
#                                       fetched at build time at a fixed revision
# The running container never downloads anything (HF_HUB_OFFLINE).

FROM python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

RUN pip install --no-cache-dir faster-whisper==1.2.1 av==18.1.0 ctranslate2==4.9.0 onnxruntime==1.32.0

ARG MODEL_REPO=Systran/faster-whisper-small
ARG MODEL_REVISION=536b0662742c02347bc0e980a01041f333bce120
RUN python -c "from huggingface_hub import snapshot_download; \
snapshot_download('${MODEL_REPO}', revision='${MODEL_REVISION}', local_dir='/models/whisper')" \
 && rm -rf /models/whisper/.cache /root/.cache \
 && chmod -R a+rX /models

ENV HF_HUB_OFFLINE=1 \
    VOICE_MODEL=/models/whisper \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="logos-voice" \
      org.opencontainers.image.description="Speech-to-text for Logos agents (faster-whisper, Whisper small)" \
      org.opencontainers.image.source="https://github.com/GregsGreyCode/Logos"

USER 10000:10000
