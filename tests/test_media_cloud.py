"""The R2 layout of the album.

The bucket holds one folder per workspace and one folder per owner inside it.
These checks do not touch the network: they only describe the keys.
"""
from services import media_cloud


def test_folder_name_is_safe():
    assert media_cloud.folder("@hiu9h") == "@hiu9h"
    assert media_cloud.folder("Cộng tác viên 01") == "C-ng-t-c-vi-n-01"
    assert media_cloud.folder("../../etc") == "etc"
    assert media_cloud.folder("") == media_cloud.SHARED_SCOPE


def test_remote_key_follows_workspace_and_owner():
    key = media_cloud.remote_key("@hiu9h", "hiu9h", "1/abcdef0123456789.jpg")
    assert key == "@hiu9h/hiu9h/abcdef0123456789.jpg"


def test_remote_key_of_the_shared_pool():
    key = media_cloud.remote_key("@hiu9h", "", "1/abcdef0123456789.jpg")
    assert key == "@hiu9h/chung/abcdef0123456789.jpg"


def test_remote_key_keeps_the_thumbnail_folder():
    key = media_cloud.remote_key("@hiu9h", "hiu9h", "1/thumbs/abcdef0123456789.jpg")
    assert key == "@hiu9h/hiu9h/thumbs/abcdef0123456789.jpg"


def test_remote_key_never_leaves_the_bucket():
    key = media_cloud.remote_key("@hiu9h", "hiu9h", "1/../../secret.jpg")
    assert ".." not in key
    assert key.startswith("@hiu9h/")
