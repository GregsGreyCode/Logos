"""Build-time helper for infra/stt/Dockerfile: fetch a file and refuse it unless its SHA-256 matches,
and pick the reference voices out of LibriSpeech. Never used by the running container."""
import hashlib
import os
import sys
import tarfile
import urllib.request


def fetch(url: str, sha256: str, dest: str) -> None:
    h = hashlib.sha256()
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        while True:
            block = r.read(1 << 20)
            if not block:
                break
            h.update(block)
            f.write(block)
    if h.hexdigest() != sha256:
        os.remove(dest)
        sys.exit(f"{url}: checksum {h.hexdigest()} is not the pinned {sha256}")


def cohort(archive: str, dest: str, per_speaker: int = 3, low: int = 120_000, high: int = 400_000) -> None:
    """``per_speaker`` utterances of each speaker, about 4 to 12 seconds long (by FLAC size), in name order."""
    os.makedirs(dest, exist_ok=True)
    picked: dict = {}
    with tarfile.open(archive, "r:gz") as tar:
        members = sorted((m for m in tar.getmembers() if m.isfile() and m.name.endswith(".flac")), key=lambda m: m.name)
        for m in members:
            speaker = m.name.split("/")[-3]
            if low <= m.size <= high and len(picked.setdefault(speaker, [])) < per_speaker:
                picked[speaker].append(m)
        for speaker, ms in picked.items():
            for m in ms:
                with tar.extractfile(m) as src, open(os.path.join(dest, os.path.basename(m.name)), "wb") as out:
                    out.write(src.read())
    print(f"{sum(len(v) for v in picked.values())} clips of {len(picked)} speakers")


def untar(archive: str, dest: str) -> None:
    """The model's files, without the folder they are packed in or its example recordings."""
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        for m in tar.getmembers():
            name = m.name.split("/", 1)[1] if "/" in m.name else ""
            if not m.isfile() or not name or "/" in name:
                continue
            with tar.extractfile(m) as src, open(os.path.join(dest, name), "wb") as out:
                out.write(src.read())


if __name__ == "__main__":
    if sys.argv[1] == "fetch":
        fetch(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == "untar":
        untar(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "cohort":
        cohort(sys.argv[2], sys.argv[3])
