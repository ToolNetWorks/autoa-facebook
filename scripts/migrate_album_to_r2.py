"""Copy the pictures of the local album into the bucket of R2.

Pictures that were uploaded before the cloud was configured only exist on the
disk of the server. This script sends them to the bucket and keeps the local
copy as a fallback until the migration is done.
"""
import os
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

from services import media_cloud  # noqa: E402

DATA = pathlib.Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DB = DATA / "manager.sqlite3"


def local_path(storage_key: str) -> pathlib.Path:
    root = (DATA / "album").resolve()
    target = (root / storage_key).resolve()
    if not str(target).startswith(str(root) + os.sep):
        return pathlib.Path("/nonexistent")
    return target


def main() -> int:
    if not media_cloud.enabled():
        print("R2 chua duoc cau hinh")
        return 1
    media_cloud.ensure_bucket()
    connection = sqlite3.connect(str(DB))
    connection.row_factory = sqlite3.Row
    rows = connection.execute("""SELECT m.*, p.name pipeline_name,
                                         (SELECT u.username FROM users u WHERE u.id=m.owner_user_id) owner_name
                                  FROM media_assets m JOIN pipelines p ON p.id=m.pipeline_id""").fetchall()
    sent = skipped = 0
    for row in rows:
        scope = row["owner_name"] or media_cloud.SHARED_SCOPE
        media_cloud.ensure_folder(media_cloud.folder(row["pipeline_name"]))
        for key, content_type in ((row["storage_key"], row["mime_type"] or "image/jpeg"),
                                  (row["thumbnail_key"], "image/jpeg")):
            if not key:
                continue
            remote = media_cloud.remote_key(row["pipeline_name"], scope, key)
            if media_cloud.get_bytes(remote) is not None:
                skipped += 1
                continue
            source = local_path(key)
            if not source.is_file():
                continue
            media_cloud.put_bytes(remote, source.read_bytes(), content_type)
            sent += 1
    connection.close()
    print(f"da gui {sent} tep len R2, {skipped} tep da co")
    return 0


if __name__ == "__main__":
    sys.exit(main())
