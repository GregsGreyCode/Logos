# Logos speech-to-text for the house speaker, and who said it (Voice VO.1).
#
# Home Assistant sends what the speaker heard over the Wyoming protocol and gets the words back
# (gateway/stt.py, mounted read-only at start with gateway/voiceprint.py; gateway/stt_host.py runs
# it). From the same audio it makes a voice embedding the dashboard compares with the voice prints
# it holds. It holds no voice prints, no audio and no agent tokens.
#
# Third-party parts:
#   sherpa-onnx 2.0.1                  runs both models (Apache-2.0; it carries its own onnxruntime)
#   NVIDIA Parakeet TDT 0.6B v2, int8  English speech to text (CC-BY-4.0), as packaged by sherpa-onnx:
#                                      the model Home Assistant's Whisper add-on picks by default for
#                                      English. Pinned by SHA-256.
#   3D-Speaker CAM++ (zh_en common advanced), from sherpa-onnx's model releases, pinned by SHA-256
#                                      (the model the wake-word spike used)
#   av 18.1.0, numpy 2.3.4             reading browser recordings (the voice service's av pin)
#   wyoming 1.11.0                     the protocol Home Assistant speaks to voice services (MIT)
#   LibriSpeech dev-clean (openslr.org, CC BY 4.0), pinned by SHA-256: three short clips of each of
#                                      its 40 speakers are kept as the reference voices
# Everything is fetched at build time; the running container never downloads anything.

FROM python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base
RUN pip install --no-cache-dir sherpa-onnx==2.0.1 sherpa-onnx-core==2.0.1 av==18.1.0 numpy==2.3.4 wyoming==1.11.0

FROM base AS cohort
COPY fetch.py /build/fetch.py
ARG LIBRISPEECH=https://www.openslr.org/resources/12/dev-clean.tar.gz
ARG LIBRISPEECH_SHA256=76f87d090650617fca0cac8f88b9416e0ebf80350acb97b343a85fa903728ab3
RUN python /build/fetch.py fetch "${LIBRISPEECH}" "${LIBRISPEECH_SHA256}" /build/dev-clean.tar.gz \
 && python /build/fetch.py cohort /build/dev-clean.tar.gz /cohort \
 && rm /build/dev-clean.tar.gz

FROM base AS models
COPY fetch.py /build/fetch.py
ARG WORDS_MODEL=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2
ARG WORDS_MODEL_SHA256=6fc4426b54aa549a0019b8a75b8dea59164e71790a37ac56a045ad57f915f714
ARG SPEAKER_MODEL=https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx
ARG SPEAKER_MODEL_SHA256=53cab20ba8f2eb2f6e2d89ecc18e88ef3222169b61b4419cbc13666744a4574b
RUN python /build/fetch.py fetch "${WORDS_MODEL}" "${WORDS_MODEL_SHA256}" /build/words.tar.bz2 \
 && python /build/fetch.py untar /build/words.tar.bz2 /models/words && rm /build/words.tar.bz2 \
 && python /build/fetch.py fetch "${SPEAKER_MODEL}" "${SPEAKER_MODEL_SHA256}" /models/campplus.onnx

FROM base
COPY --from=models /models /models
COPY --from=cohort /cohort /models/cohort
RUN chmod -R a+rX /models

ENV STT_WORDS=/models/words \
    STT_SPEAKER=/models/campplus.onnx \
    STT_COHORT=/models/cohort \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="logos-stt" \
      org.opencontainers.image.description="Speech to text for the Logos house speaker, and who said it (Parakeet, CAM++, Wyoming)" \
      org.opencontainers.image.source="https://github.com/GregsGreyCode/Logos"

USER 10000:10000
