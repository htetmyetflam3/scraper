"""
Description:
  Standalone Zawgyi/Unicode text detector using Google's myanmar-tools
  binary Markov model (zawgyiUnicodeModel.dat). No external dependencies.

  The model file is searched in this order:
  1. Path explicitly passed to ZawgyiDetector(model_path=...)
  2. Same directory as this module file
  3. Current working directory
  4. Directory of the calling script (sys.argv[0])

  Usage:
      from zawgyi_detector import ZawgyiDetector
      detector = ZawgyiDetector()  # auto-finds model
      # or
      detector = ZawgyiDetector("/path/to/zawgyiUnicodeModel.dat")
"""

from bisect import bisect_left
from itertools import chain
from math import exp, inf, isnan, nan
from pathlib import Path
import struct
import sys
from array import array
from itertools import repeat
from typing import BinaryIO, Iterator, Tuple

# Myanmar Unicode character ranges
STD = range(0x1000, 0x103F + 1)
AFT = range(0x104A, 0x109F + 1)
EXA = range(0xAA60, 0xAA7F + 1)
EXB = range(0xA9E0, 0xA9FF + 1)
SPC = range(0x2000, 0x200B + 1)


def _read_int(stream: BinaryIO) -> int:
    return struct.unpack('>i', stream.read(4))[0]


def _read_short(stream: BinaryIO) -> int:
    return struct.unpack('>h', stream.read(2))[0]


def _read_float(stream: BinaryIO) -> float:
    return struct.unpack('>f', stream.read(4))[0]


def _read_pairs(stream: BinaryIO, n: int) -> Iterator[Tuple[int, float]]:
    return struct.iter_unpack('>hf', stream.read(6 * n))


def _find_model(model_path: str | Path | None) -> Path:
    """Resolve model file path with multiple fallbacks."""
    if model_path is not None:
        p = Path(model_path)
        if p.exists():
            return p
        raise FileNotFoundError(f"Model file not found: {p}")

    candidates = [
        # the SHARED copy: <root>/model, same file the JS parser reads
        # (Parser/parsed/core/paths.mjs DEFAULT_MODEL_PATH)
        Path(__file__).resolve().parent.parent / "model" / "zawgyiUnicodeModel.dat",
        Path(__file__).with_suffix('').parent / "zawgyiUnicodeModel.dat",
        Path.cwd() / "zawgyiUnicodeModel.dat",
        Path(sys.argv[0]).parent / "zawgyiUnicodeModel.dat",
    ]
    for cand in candidates:
        if cand.exists():
            return cand

    raise FileNotFoundError(
        "zawgyiUnicodeModel.dat not found. Searched:\n"
        + "\n".join(f"  - {c}" for c in candidates)
        + "\nPlace the .dat file next to this script or pass model_path="
    )


class ZawgyiDetector:
    """Detects whether Myanmar text is encoded in Zawgyi or Unicode."""

    def __init__(self, model_path: str | Path | None = None) -> None:
        """
        Load the binary Markov model.

        Parameters
        ----------
        model_path : str | Path | None
            Path to zawgyiUnicodeModel.dat. If None, auto-searches common
            locations (same dir as module, cwd, script dir).
        """
        model_path = _find_model(model_path)

        with open(model_path, "rb") as stream:
            # UZMODEL header
            uzmodel_tag = stream.read(8)
            if uzmodel_tag != b'UZMODEL ':
                raise IOError("invalid uzmodel_tag")

            uzmodel_version = _read_int(stream)
            if uzmodel_version == 1:
                ssv = 0
            elif uzmodel_version == 2:
                ssv = _read_int(stream)
            else:
                raise IOError("invalid uzmodel_version")

            if ssv == 0:
                chars = ''.join(map(chr, chain(STD, AFT, EXA, EXB, SPC)))
            elif ssv == 1:
                chars = ''.join(map(chr, chain(STD, AFT, EXA, EXB)))
            else:
                raise ValueError("invalid ssv")

            # BMARKOV header
            bmarkov_tag = stream.read(8)
            if bmarkov_tag != b'BMARKOV ':
                raise IOError("invalid bmarkov_tag")

            bmarkov_version = _read_int(stream)
            if bmarkov_version != 0:
                raise IOError("invalid bmarkov_version")

            # Read params matrix
            size = _read_short(stream)
            params = array('f', repeat(0.0, size * size))
            for i in range(size):
                count = _read_short(stream)
                if count != 0:
                    offset = i * size
                    base_value = _read_float(stream)
                    for j in range(size):
                        params[offset + j] = base_value
                    for index, value in _read_pairs(stream, count):
                        params[offset + index] = value

        self._chars = chars
        self._params = params
        self._size = size
        # Node 0 is for foreign characters — mark as NaN
        self._params[0] = nan

    def _state(self, char: str | None) -> int:
        """Return state index for a character (0 = foreign/unknown)."""
        if char is None:
            return 0
        i = bisect_left(self._chars, char)
        if i < len(self._chars) and self._chars[i] == char:
            return i + 1
        return 0

    def _llrs(self, string: str) -> Iterator[float]:
        """Yield log-likelihood ratios for consecutive character pairs."""
        size = self._size
        return (
            self._params[self._state(i) * size + self._state(j)]
            for i, j in zip(chain((None,), string), chain(string, (None,)))
        )

    def get_zawgyi_probability(self, string: str) -> float:
        """
        Return probability that `string` is Zawgyi-encoded.

        Returns
        -------
        float
            0.0–1.0 probability. Values > 0.5 strongly suggest Zawgyi.
            Returns -inf if string contains only foreign characters.
        """
        if all(map(isnan, self._llrs(string))):
            return -inf
        total = sum(x for x in self._llrs(string) if not isnan(x))
        if total >= 0:
            z = exp(-total)
            return z / (z + 1)
        return 1 / (1 + exp(total))

    def detect(self, string: str, threshold: float = 0.5) -> tuple[str, float]:
        """
        Convenience wrapper: returns ("ZAWGYI" | "UNICODE" | "UNKNOWN", probability).

        Parameters
        ----------
        string : str
            Text to analyse.
        threshold : float
            Probability cutoff. Default 0.5.
        """
        prob = self.get_zawgyi_probability(string)
        if prob == -inf:
            return "UNKNOWN", prob
        if prob > threshold:
            return "ZAWGYI", prob
        return "UNICODE", prob
