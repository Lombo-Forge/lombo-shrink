"""A skip list for the candidate scan, and a bigger free-space margin for one disk."""

import pytest

from conftest import unix_only
from lombo_shrink import scan, swap
from lombo_shrink.config import ConfigError, from_env
from test_swap import Setup


def test_skip_list_matches_files_and_folders_only_whole_names(tmp_path):
    f = tmp_path / "skip.txt"
    f.write_text("# going soon\n\nmovies/Old (1999)/\ntv\\Show\\Season 02\n", encoding="utf-8")
    entries = scan.skipped_paths(str(f))
    assert entries == ("movies/Old (1999)", "tv/Show/Season 02")
    assert scan.listed("movies/Old (1999)/a.mkv", entries)
    assert not scan.listed("movies/Old (1999) Remake/a.mkv", entries)
    assert scan.skipped_paths("") == ()
    with pytest.raises(OSError, match="nope"):
        scan.skipped_paths(str(tmp_path / "nope"))


def test_branch_margin_must_name_a_library_folder():
    base = {"LOMBO_SHRINK_BRANCHES": "/a:/b"}
    assert from_env({**base, "LOMBO_SHRINK_BRANCH_MARGIN_GB": "/a/=250"}).branch_margin_gb == (("/a", 250.0),)
    for bad in ("/c=5", "/a", "/a=x", "/a=-1", "/a=nan"):
        with pytest.raises(ConfigError, match="BRANCH_MARGIN_GB"):
            from_env({**base, "LOMBO_SHRINK_BRANCH_MARGIN_GB": bad})


@unix_only
def test_a_disks_own_margin_stops_a_swap(tmp_path):
    setup = Setup(tmp_path)
    roomy = setup.cfg()
    full = from_env({**setup.env, "LOMBO_SHRINK_BRANCH_MARGIN_GB": f"{setup.lib}=100000"})
    assert swap.swap_one(roomy, setup.entry(), apply=False).status == "swapped"
    result = swap.swap_one(full, setup.entry(), apply=False)
    assert (result.status, result.reason) == ("failed", "not enough free space on the disk holding the original")
