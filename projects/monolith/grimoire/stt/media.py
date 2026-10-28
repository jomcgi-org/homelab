"""Bounded multipart and media decoding, entirely in managed memory."""

import asyncio
import io
import re
import struct
from email import policy
from email.parser import BytesParser

from fastapi import HTTPException

MAX_AUDIO_BYTES = 1_000_000
MAX_FRAMING_BYTES = 8192
MAX_SECONDS = 15


def _webm_boundaries(audio):
    """Validate EBML extents, including unknown-sized streaming clusters.

    Demuxers deliberately tolerate an interrupted recording. The upload
    contract requires every declared finite element to be present instead.
    Unknown size is allowed only for Segment and Cluster, as used by browser
    MediaRecorder. Actual codec and duration are still checked by decoding.
    """
    masters = {
        0x1A45DFA3,
        0x18538067,
        0x114D9B74,
        0x4DBB,
        0x1549A966,
        0x1654AE6B,
        0xAE,
        0xE1,
        0x1F43B675,
        0xA0,
        0x1C53BB6B,
        0xBB,
        0xB7,
        0x1254C367,
        0x7373,
        0x63C0,
        0x67C8,
    }
    top_level = {
        0x114D9B74,
        0x1549A966,
        0x1654AE6B,
        0x1F43B675,
        0x1C53BB6B,
        0x1254C367,
        0x1941A469,
        0x1043A770,
    }
    nodes = 0
    doc_type = False
    segments = 0

    def vint(offset, end, *, identifier=False):
        if offset >= end or audio[offset] == 0:
            raise ValueError()
        size = 9 - audio[offset].bit_length()
        if size > (4 if identifier else 8) or offset + size > end:
            raise ValueError()
        raw = int.from_bytes(audio[offset : offset + size], "big")
        value = raw if identifier else raw & ((1 << (7 * size)) - 1)
        unknown = not identifier and value == (1 << (7 * size)) - 1
        return value, offset + size, unknown

    def scan(offset, end, depth=0, streaming_cluster=False):
        nonlocal nodes, doc_type, segments
        if depth > 10:
            raise ValueError()
        while offset < end:
            identity, after_id, _ = vint(offset, end, identifier=True)
            if streaming_cluster and identity in top_level:
                return offset
            length, start, unknown = vint(after_id, end)
            nodes += 1
            if nodes > 10000 or (unknown and identity not in {0x18538067, 0x1F43B675}):
                raise ValueError()
            stop = end if unknown else start + length
            if stop > end:
                raise ValueError()
            if identity == 0x4282:
                doc_type = audio[start:stop] == b"webm"
            if identity == 0x18538067:
                segments += 1
                if depth != 0 or segments > 1:
                    raise ValueError()
            if identity in masters:
                offset = scan(
                    start,
                    stop,
                    depth + 1,
                    streaming_cluster=unknown and identity == 0x1F43B675,
                )
            else:
                offset = stop
        return offset

    scan(0, len(audio))
    if not doc_type or segments != 1:
        raise ValueError()


async def read_audio(request):
    """Do not declare File/UploadFile: Starlette would spool before this check."""
    content_type = request.headers.get("content-type", "")
    if len(content_type) > 256 or not re.fullmatch(
        r'multipart/form-data;\s*boundary=(?:[A-Za-z0-9_-]{1,70}|"[A-Za-z0-9_-]{1,70}")',
        content_type,
    ):
        raise HTTPException(415, "one multipart audio field required")
    payload = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(payload) + len(chunk) > MAX_AUDIO_BYTES + MAX_FRAMING_BYTES:
                    raise HTTPException(413, "audio request too large")
                payload.extend(chunk)
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + payload
        )
        parts = list(message.iter_parts())
        if message.defects or len(parts) != 1:
            raise HTTPException(422, "malformed multipart audio")
        part = parts[0]
        if (
            part.defects
            or part.is_multipart()
            or part.get_param("name", header="content-disposition") != "audio"
            or part.get_content_disposition() != "form-data"
            or part.get("content-transfer-encoding") is not None
        ):
            raise HTTPException(422, "one binary audio field required")
        mime = part.get_content_type()
        if mime == "audio/webm" and part.get_param("codecs") == "opus":
            mime = "audio/webm;codecs=opus"
        elif mime != "audio/wav":
            raise HTTPException(415, "use WAV PCM16 or WebM/Opus")
        audio = part.get_payload(decode=True)
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "audio must be 1 to 1000000 bytes")
        if len(payload) - len(audio) > MAX_FRAMING_BYTES:
            raise HTTPException(413, "multipart framing too large")
        return audio, mime
    except TimeoutError:
        raise HTTPException(408, "audio upload timed out") from None
    finally:
        payload.clear()
        # Reference release, not secure erasure in managed memory.
        message = part = parts = chunk = None


def validate_media(audio, mime):
    """Decode actual samples and timestamps; never trust container duration.

    Only one audio stream is supported. The bounded input and sample count
    bound decoder work. PyAV reads a BytesIO, never a filename or URL. No
    FFmpeg subprocess, cache, device, network input or temporary file is used.
    """
    import av

    invalid = False
    seconds = 0.0
    try:
        if not 0 < len(audio) <= MAX_AUDIO_BYTES:
            raise ValueError()
        if mime == "audio/wav":
            if (
                audio[:4] != b"RIFF"
                or audio[8:12] != b"WAVE"
                or len(audio) < 44
                or struct.unpack_from("<I", audio, 4)[0] + 8 != len(audio)
            ):
                raise ValueError()
            # wave checks PCM headers; reading the declared frames detects
            # truncated data that FFmpeg may otherwise accept as a short clip.
            import wave

            with wave.open(io.BytesIO(audio), "rb") as wav:
                if (
                    wav.getsampwidth() != 2
                    or wav.getnchannels() not in {1, 2}
                    or not 8000 <= wav.getframerate() <= 48000
                ):
                    raise ValueError()
                frames = wav.getnframes()
                if not 0 < frames <= MAX_SECONDS * wav.getframerate():
                    raise ValueError()
                if len(wav.readframes(frames)) != frames * 2 * wav.getnchannels():
                    raise ValueError()
            format_name, codec = "wav", "pcm_s16le"
        elif mime == "audio/webm;codecs=opus" and audio.startswith(b"\x1aE\xdf\xa3"):
            _webm_boundaries(audio)
            format_name, codec = "matroska", "opus"
        else:
            raise ValueError()
        with (
            av.logging.Capture(local=True),
            av.open(
                io.BytesIO(audio), format=format_name, options={"err_detect": "explode"}
            ) as container,
        ):
            if len(container.streams) != 1 or len(container.streams.audio) != 1:
                raise ValueError()
            stream = container.streams.audio[0]
            if (
                stream.codec_context.name != codec
                or stream.codec_context.channels not in {1, 2}
            ):
                raise ValueError()
            samples = 0
            start = None
            for packet in container.demux(stream):
                if packet.is_corrupt:
                    raise ValueError()
                for frame in packet.decode():
                    if frame.is_corrupt or not frame.sample_rate:
                        raise ValueError()
                    samples += frame.samples
                    seconds = samples / frame.sample_rate
                    if seconds > MAX_SECONDS:
                        raise ValueError()
                    if frame.pts is not None:
                        timestamp = float(frame.pts * frame.time_base)
                        if start is None:
                            start = timestamp
                        elapsed = timestamp - start + frame.samples / frame.sample_rate
                        # WebM timestamps are quantized to its stream timebase
                        # (usually milliseconds); decoded sample count is exact.
                        if elapsed < 0 or elapsed > MAX_SECONDS + float(
                            stream.time_base
                        ):
                            raise ValueError()
            if seconds <= 0:
                raise ValueError()
    except Exception:  # noqa: BLE001 - decoder exceptions may contain media metadata
        invalid = True
    finally:
        audio = None
    if invalid:
        raise HTTPException(422, "invalid, truncated or over-duration audio") from None
    return seconds
