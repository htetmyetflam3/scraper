"""Offline tests for linearize.py.

They build real PDFs with pikepdf in a temporary directory, so nothing here
touches the network or the actual pdfs/ folder.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/

from linearize import (  # noqa: E402
    TMP_SUFFIX,
    choose_keeper,
    find_duplicates,
    is_pdf,
    iter_files,
    linearize,
    sha256_of,
    sweep,
)


def make_locked_pdf(path: Path, user: str = "", owner: str = "owner") -> Path:
    """An encrypted PDF. user="" is the restrictions-only kind (no password to open)."""
    import pikepdf

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    pdf.save(path, encryption=pikepdf.Encryption(owner=owner, user=user, R=6))
    return path


def is_encrypted(path: Path, password: str = "") -> bool:
    import pikepdf

    with pikepdf.open(path, password=password) as pdf:
        return bool(pdf.is_encrypted)


def make_pdf(path: Path, pages: int = 1, linear: bool = False, title: str | None = None) -> Path:
    """Blank PDFs are byte-identical, so pass a title when you need distinct files."""
    import pikepdf

    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page()
    if title:
        with pdf.open_metadata() as meta:
            meta["dc:title"] = title
    pdf.save(path, linearize=linear)
    return path


def test_make_pdf_is_not_linearized_until_asked(tmp_path):
    plain = make_pdf(tmp_path / "plain.pdf")
    assert not (b"/Linearized" in plain.read_bytes()[:4096])
    linear = make_pdf(tmp_path / "linear.pdf", linear=True)
    assert b"/Linearized" in linear.read_bytes()[:4096]


def test_is_pdf_reads_magic_bytes(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf")
    assert is_pdf(pdf)
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"<html><body>login required</body></html>")
    assert not is_pdf(fake)


def test_linearize_rewrites_the_file_in_place(tmp_path):
    path = make_pdf(tmp_path / "book.pdf")
    before = sha256_of(path)
    outcome = linearize(path)

    assert outcome.state == "linearized"
    assert b"/Linearized" in path.read_bytes()[:4096]
    assert sha256_of(path) != before, "the bytes must actually change"
    assert len(list(tmp_path.glob(f"*{TMP_SUFFIX}"))) == 0, "no temp file left behind"
    assert len(list(tmp_path.iterdir())) == 1, "no copy left behind"


def test_linearize_keeps_the_page_count(tmp_path):
    path = make_pdf(tmp_path / "book.pdf", pages=3)
    linearize(path)
    import pikepdf

    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 3


def test_linearize_reports_broken_files_instead_of_destroying_them(tmp_path):
    path = tmp_path / "truncated.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not really a pdf")
    original = path.read_bytes()

    outcome = linearize(path)

    assert outcome.state in ("broken", "failed")
    assert path.read_bytes() == original, "a file that cannot be linearized keeps its bytes"


def test_sweep_deletes_duplicates_and_keeps_one(tmp_path):
    keeper = make_pdf(tmp_path / "a.pdf", title="a")
    duplicate = tmp_path / "a (1).pdf"
    duplicate.write_bytes(keeper.read_bytes())
    duplicate_size = duplicate.stat().st_size
    other = make_pdf(tmp_path / "b.pdf", pages=2, title="b")

    report = sweep(tmp_path, quiet=True)

    assert not duplicate.exists()
    assert keeper.exists()
    assert other.exists()
    assert len(list(tmp_path.iterdir())) == 2
    assert report.freed == duplicate_size


def test_sweep_linearizes_every_pdf_it_finds(tmp_path):
    for name in ("one.pdf", "two.pdf", "three.pdf"):
        make_pdf(tmp_path / name, title=name)

    report = sweep(tmp_path, quiet=True)

    assert report.count("linearized") == 3
    assert len(list(tmp_path.iterdir())) == 3, "still three files, no backups"
    for path in tmp_path.iterdir():
        assert b"/Linearized" in path.read_bytes()[:4096]


def test_sweep_skips_files_that_only_look_like_pdfs(tmp_path):
    make_pdf(tmp_path / "real.pdf")
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"<html>login</html>")

    report = sweep(tmp_path, quiet=True)

    assert report.count("skipped") == 1
    assert fake.exists(), "a non-PDF is reported, never silently deleted"
    assert report.failures[0].path == fake


def test_sweep_leaves_broken_files_unless_asked(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnope")

    report = sweep(tmp_path, quiet=True)
    assert broken.exists()
    assert report.failures

    report = sweep(tmp_path, delete_broken=True, quiet=True)
    assert not broken.exists()


def test_sweep_does_not_touch_already_linearized_files(tmp_path):
    path = make_pdf(tmp_path / "done.pdf", linear=True, title="done")
    before = sha256_of(path)

    report = sweep(tmp_path, quiet=True)

    assert report.count("already") == 1
    assert sha256_of(path) == before, "rewriting an already-linearized file is pointless"


def test_dry_run_changes_nothing(tmp_path):
    keeper = make_pdf(tmp_path / "a.pdf", title="a")
    duplicate = tmp_path / "copy.pdf"
    duplicate.write_bytes(keeper.read_bytes())
    hashes = {p.name: sha256_of(p) for p in tmp_path.iterdir()}

    report = sweep(tmp_path, dry_run=True, quiet=True)

    assert {p.name: sha256_of(p) for p in tmp_path.iterdir()} == hashes
    assert duplicate.exists()
    assert report.count("linearized") == 1, "the duplicate is not counted twice"
    assert len(report.removed) == 1 and report.freed == duplicate.stat().st_size


def test_recursive_descends_into_subdirectories(tmp_path):
    nested = tmp_path / "set" / "volume-1"
    nested.mkdir(parents=True)
    make_pdf(nested / "deep.pdf")

    assert iter_files(tmp_path, recursive=False) == []
    assert [p.name for p in iter_files(tmp_path, recursive=True)] == ["deep.pdf"]

    sweep(tmp_path, recursive=True, quiet=True)
    assert b"/Linearized" in (nested / "deep.pdf").read_bytes()[:4096]


def test_choose_keeper_prefers_the_tidiest_name(tmp_path):
    paths = [tmp_path / "Myanmar Poems (1).pdf", tmp_path / "Myanmar Poems.pdf", tmp_path / "b.pdf"]
    keeper, duplicates = choose_keeper(paths)
    assert keeper.name == "b.pdf"
    assert sorted(p.name for p in duplicates) == ["Myanmar Poems (1).pdf", "Myanmar Poems.pdf"]


def test_find_duplicates_groups_by_content(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", title="a")
    b = tmp_path / "b.pdf"
    b.write_bytes(a.read_bytes())
    c = make_pdf(tmp_path / "c.pdf", pages=5)

    groups = find_duplicates([a, b, c])

    assert len(groups) == 1
    assert sorted(p.name for p in next(iter(groups.values()))) == ["a.pdf", "b.pdf"]


# --------------------------------------------------------------------------- #
# Encrypted PDFs are unlocked, not preserved
# --------------------------------------------------------------------------- #

def test_restrictions_only_pdf_is_unlocked_and_linearized(tmp_path):
    path = make_locked_pdf(tmp_path / "locked.pdf", user="")
    assert is_encrypted(path), "the fixture really is encrypted"

    report = sweep(tmp_path, quiet=True)

    assert report.count("linearized") == 1
    assert not is_encrypted(path), "restrictions must not survive the rewrite"
    assert b"/Linearized" in path.read_bytes()[:4096]


def test_password_protected_pdf_is_unlocked_with_the_password(tmp_path):
    path = make_locked_pdf(tmp_path / "secret.pdf", user="缅甸")
    assert is_encrypted(path, password="缅甸")

    report = sweep(tmp_path, quiet=True, passwords=("缅甸",))

    assert report.count("linearized") == 1
    assert not is_encrypted(path)
    assert b"/Linearized" in path.read_bytes()[:4096]
    assert report.outcomes[0].detail == "unlocked"


def test_locked_pdf_is_kept_when_no_password_fits(tmp_path):
    path = make_locked_pdf(tmp_path / "secret.pdf", user="hunter2")
    original = path.read_bytes()

    report = sweep(tmp_path, quiet=True)

    assert report.count("locked") == 1
    assert path.read_bytes() == original, "an unlockable file is left exactly as it was"
    assert is_encrypted(path, password="hunter2"), "still encrypted, just not unlockable"


def test_second_password_in_the_list_is_tried(tmp_path):
    path = make_locked_pdf(tmp_path / "secret.pdf", user="right-one")
    report = sweep(tmp_path, quiet=True, passwords=("wrong", "also-wrong", "right-one"))
    assert report.count("linearized") == 1
    assert not is_encrypted(path)


def test_dry_run_reports_the_unlock(tmp_path):
    path = make_locked_pdf(tmp_path / "locked.pdf", user="")
    report = sweep(tmp_path, dry_run=True, quiet=True)
    assert report.outcomes[0].state == "linearized"
    assert report.outcomes[0].detail == "would unlock"
    assert is_encrypted(path), "dry run changed nothing"


def test_encrypted_pdf_is_rewritten_even_if_already_linearized(tmp_path):
    """A locked file has to be rewritten to be unlocked, linearized or not."""
    import pikepdf

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    path = tmp_path / "both.pdf"
    pdf.save(path, linearize=True, encryption=pikepdf.Encryption(owner="owner", user="", R=6))

    sweep(tmp_path, quiet=True)

    assert not is_encrypted(path)
    assert b"/Linearized" in path.read_bytes()[:4096]
