"""Static-asset setup works with both shared and separate preview public paths."""
import pytest

from onlyfans_demo import copy_public_assets


ASSETS = {
    "brand/platforms/onlyfans.svg": b"synthetic platform icon",
    "moxy/logo.svg": b"synthetic Moxy logo",
    "lynq/logo.svg": b"synthetic LYNQ logo",
    "icon.png": b"synthetic icon",
    "brand/catalogue/approved-blue-badge.png": b"synthetic badge",
}


def seed(dashboard):
    for name, body in ASSETS.items():
        path = dashboard / "public" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def test_separate_legacy_public_receives_the_shell_assets(tmp_path):
    seed(tmp_path)
    preview = tmp_path / "preview/onlyfans-integration/public"
    retained = preview / "moxy/retained.svg"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"retained preview asset")
    copy_public_assets(tmp_path)
    assert {name: (preview / name).read_bytes() for name in ASSETS} == ASSETS
    assert retained.read_bytes() == b"retained preview asset"


@pytest.mark.parametrize("linked", ["public", "asset"])
def test_symlinked_assets_are_not_copied_onto_themselves(tmp_path, monkeypatch, linked):
    seed(tmp_path)
    preview = tmp_path / "preview/onlyfans-integration/public"
    preview.parent.mkdir(parents=True)
    if linked == "public":
        preview.symlink_to("../../public", target_is_directory=True)
    else:
        preview.mkdir()
        for name in ["brand/platforms", "moxy", "lynq", "icon.png", "brand/catalogue/approved-blue-badge.png"]:
            path = preview / name
            path.parent.mkdir(parents=True, exist_ok=True)
            source = tmp_path / "public" / name
            path.symlink_to(source, target_is_directory=source.is_dir())

    def unexpected_copy(*args, **kwargs):
        raise AssertionError("the shared asset must not be copied onto itself")

    monkeypatch.setattr("onlyfans_demo.shutil.copytree", unexpected_copy)
    monkeypatch.setattr("onlyfans_demo.shutil.copyfile", unexpected_copy)
    copy_public_assets(tmp_path)
    assert {name: (preview / name).read_bytes() for name in ASSETS} == ASSETS
    assert {name: (tmp_path / "public" / name).read_bytes() for name in ASSETS} == ASSETS
