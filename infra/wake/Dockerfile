# Logos wake words: the house speaker wakes to each agent's name (Voice VW.1, "Logos streaming").
#
# The speaker streams what it hears to Home Assistant, which passes it to this container over the
# Wyoming protocol (gateway/wake.py, mounted read-only at start; gateway/wake_host.py runs it). It
# listens for the agents' names ("Finn", "Hey Finn", "Hey Home") with an open-vocabulary keyword
# spotter, so a new or changed name needs no training. It keeps no audio and holds no tokens.
#
# Third-party parts:
#   sherpa-onnx 2.0.1        k2-fsa's speech toolkit, for its keyword spotter (Apache-2.0)
#   sentencepiece 0.2.2      turns a name into the model's word pieces (Apache-2.0)
#   wyoming 1.11.0           the protocol Home Assistant speaks to voice services (MIT)
#   sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01
#                            the keyword model (English, trained on GigaSpeech; Apache-2.0), fetched
#                            at build time from k2-fsa's release and checked against its SHA-256
# The running container never downloads anything.

FROM python@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

RUN pip install --no-cache-dir sherpa-onnx==2.0.1 sentencepiece==0.2.2 wyoming==1.11.0 && rm -rf /root/.cache

ARG KWS_MODEL=sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01
ARG KWS_SHA256=f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a
RUN python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/kws.tar.bz2')" \
      "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/${KWS_MODEL}.tar.bz2" \
    && echo "${KWS_SHA256}  /tmp/kws.tar.bz2" | sha256sum -c - \
    && mkdir -p /model && python -c "import tarfile; tarfile.open('/tmp/kws.tar.bz2').extractall('/tmp', filter='data')" \
    && cd "/tmp/${KWS_MODEL}" \
    && cp bpe.model tokens.txt README.md /model/ \
    && for part in encoder decoder joiner; do cp "${part}-epoch-12-avg-2-chunk-16-left-64.onnx" "/model/${part}.onnx"; done \
    && cd / && rm -rf /tmp/kws.tar.bz2 "/tmp/${KWS_MODEL}" && chmod -R a+rX /model

ENV WAKE_MODEL=/model \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="logos-wake" \
      org.opencontainers.image.description="Wake words for Logos agents: their names, over Wyoming" \
      org.opencontainers.image.source="https://github.com/GregsGreyCode/Logos"

USER 10000:10000
