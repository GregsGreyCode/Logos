# Logos speaker voices: text-to-speech for the house speaker, a voice for each agent (Voice V1.2).
#
# Home Assistant asks this container to read an agent's answer aloud, over the Wyoming protocol
# (gateway/tts.py, mounted read-only at start; gateway/tts_host.py runs it). It picks the voice of
# the agent that said those words. It holds no tokens and nothing else is in it.
#
# Third-party parts:
#   piper-tts 1.8.0          the Piper speech engine, as in Home Assistant's own Piper add-on
#                            (GPL-3.0; it runs on its own in this container, Logos does not link it)
#   wyoming 1.11.0           the protocol Home Assistant speaks to voice services (MIT)
#   onnxruntime 1.32.0       what Piper runs on (the same pin as the voice service)
#   rhasspy/piper-voices     twelve English and three French "medium" voice models (UPMC has two
#                            speakers), fetched at build time at a fixed revision
# The running container never downloads anything.

FROM python@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

RUN pip install --no-cache-dir piper-tts==1.8.0 wyoming==1.11.0 onnxruntime==1.32.0 pathvalidate==3.3.1

# Keep this list the same as tts.model_files() in gateway/tts.py (tests/unit/test_tts.py checks it).
ARG VOICES_REVISION=2f358b6a560e3a913ebf509b2f3344f20b367f94
ARG VOICES="en_GB-alan-medium en_GB-alba-medium en_GB-cori-medium en_GB-jenny_dioco-medium \
en_GB-northern_english_male-medium en_US-amy-medium en_US-hfc_female-medium en_US-hfc_male-medium \
en_US-joe-medium en_US-kristin-medium en_US-lessac-medium en_US-ryan-medium \
fr_FR-siwis-medium fr_FR-tom-medium fr_FR-upmc-medium"
RUN mkdir -p /voices && cd /voices && for v in ${VOICES}; do \
      lang="${v%%-*}"; rest="${v#*-}"; name="${rest%-*}"; quality="${rest##*-}"; \
      for ext in onnx onnx.json; do \
        python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
          "https://huggingface.co/rhasspy/piper-voices/resolve/${VOICES_REVISION}/${lang%%_*}/${lang}/${name}/${quality}/${v}.${ext}" \
          "${v}.${ext}" || exit 1; \
      done; \
    done && chmod -R a+rX /voices && rm -rf /root/.cache

ENV TTS_VOICES=/voices \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="logos-tts" \
      org.opencontainers.image.description="Speaker voices for Logos agents (Piper over Wyoming)" \
      org.opencontainers.image.source="https://github.com/GregsGreyCode/Logos"

USER 10000:10000
