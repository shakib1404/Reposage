from collections import defaultdict
from pathlib import Path

import numpy as np
from copydetect import CodeFingerprint, compare_files

from .comparison import Comparator, Comparison, Slice

# A hash that occurs many times in BOTH files (a repeated import block, a run of
# identical getters) matches every occurrence against every occurrence. Those
# matches have no single "right" counterpart and would flood the result with
# guesses, so they are left out of the pairing. They still count toward the
# similarity score, which is computed by copydetect itself, unchanged.
MAX_PAIRS_PER_HASH = 8


def aligned_slices(fp1: CodeFingerprint, fp2: CodeFingerprint) -> tuple[tuple[Slice, Slice], ...]:
    """Copied regions with the counterpart each one really matched.

    Every shared k-gram hash has a position in file 1 (a) and one in file 2 (b).
    Code copied as a block shows up as k-grams that advance TOGETHER: a, a+1,
    a+2 ... in file 1 against b, b+1, b+2 ... in file 2, i.e. a constant
    difference b - a (a "diagonal"). Merging runs along one diagonal gives
    regions whose two sides are the same code, wherever each sits in its file
    and in whatever order the blocks appear.
    """
    k = fp1.k
    by_diag: dict[int, list[int]] = defaultdict(list)
    for h in fp1.hashes & fp2.hashes:
        p1, p2 = fp1.hash_idx[h], fp2.hash_idx[h]
        if len(p1) * len(p2) > MAX_PAIRS_PER_HASH:
            continue
        for a in p1:
            for b in p2:
                by_diag[int(b) - int(a)].append(int(a))

    runs: list[tuple[int, int, int]] = []          # (start in file 1, last k-gram, diagonal)
    for diag, starts in by_diag.items():
        starts.sort()
        first = last = starts[0]
        for a in starts[1:]:
            if a - last <= k - 1:                    # same merge rule as copydetect's get_copied_slices
                last = a
            else:
                runs.append((first, last, diag))
                first = last = a
        runs.append((first, last, diag))
    if not runs:
        return ()

    runs = sorted(set(runs))
    starts1 = np.array([r[0] for r in runs])
    ends1 = np.array([r[1] + k for r in runs])
    starts2 = np.array([r[0] + r[2] for r in runs])
    ends2 = np.array([r[1] + r[2] + k for r in runs])

    def to_unfiltered(fp: CodeFingerprint, arr: np.ndarray) -> np.ndarray:
        # identical conversion to the one compare_files applies to its slices
        if len(fp.offsets) > 0:
            arr = arr + fp.offsets[:, 1][np.clip(np.searchsorted(fp.offsets[:, 0], arr),
                                                 0, fp.offsets.shape[0] - 1)]
        return arr

    s1, e1 = to_unfiltered(fp1, starts1), to_unfiltered(fp1, ends1)
    s2, e2 = to_unfiltered(fp2, starts2), to_unfiltered(fp2, ends2)
    pairs = [(Slice(int(a), int(b)), Slice(int(c), int(d))) for a, b, c, d in zip(s1, e1, s2, e2)]
    return _drop_contained(sorted(set(pairs)))       # reading order of file 1


# Characters of slack when deciding that one region lies inside another; the
# edges of a run are token-aligned, not line-aligned.
_CONTAIN_SLACK = 80


def _drop_contained(pairs: list[tuple[Slice, Slice]]) -> tuple[tuple[Slice, Slice], ...]:
    """Remove a pair whose BOTH sides lie inside a larger pair's two sides.

    A block matched across two nearby diagonals (a repeated sequence shifts the
    alignment by a few tokens) would otherwise be shown twice, the second time
    as a shorter copy of the first.
    """
    def inside(inner: Slice, outer: Slice) -> bool:
        return (outer.from_index - _CONTAIN_SLACK <= inner.from_index
                and inner.to_index <= outer.to_index + _CONTAIN_SLACK)

    by_size = sorted(pairs, key=lambda p: -(p[0].to_index - p[0].from_index))
    kept: list[tuple[Slice, Slice]] = []
    for a, b in by_size:
        if any(inside(a, ka) and inside(b, kb) for ka, kb in kept):
            continue
        kept.append((a, b))
    return tuple(sorted(kept))


class CopyDetectComparator(Comparator[CodeFingerprint]):
    def fingerprint(self, path: Path) -> CodeFingerprint:
        return CodeFingerprint(str(path), k=25, win_size=1)

    def compare(self, fp1: CodeFingerprint, fp2: CodeFingerprint) -> Comparison:
        overlap, (sim1, sim2), (slice1, slice2) = compare_files(fp1, fp2)
        # Most file pairs share nothing; only the ones that do pay for pairing.
        pairs = aligned_slices(fp1, fp2) if overlap else ()
        return Comparison(
            overlap,
            sim1,
            sim2,
            tuple(Slice.from_ndarray(slice1)),
            tuple(Slice.from_ndarray(slice2)),
            pairs,
        )
