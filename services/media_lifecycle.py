"""Vòng đời của ảnh trong một chiến dịch đăng group.

Ảnh được dàn **một lần** cho chiến dịch, dùng chung cho mọi job, và được
dọn lại nếu việc tạo job thất bại. Nhờ vậy một chiến dịch 65 group không tải
cùng một ảnh 65 lần, và một thao tác hỏng không để lại ảnh rác.
"""
from __future__ import annotations

import shutil
from pathlib import Path


class MediaStaging:
    """Ảnh đã dàn cho một chiến dịch, dọn lại khi không dùng tới."""

    def __init__(self, data_root: Path, uploads_root: Path, pipeline_id: int):
        self.data_root = Path(data_root)
        self.uploads_root = Path(uploads_root)
        self.pipeline_id = int(pipeline_id)
        self.created: list[Path] = []
        self.paths: list[str] = []

    def stage(self, filename: str, resolver) -> str:
        """Dàn một ảnh cho chiến dịch và trả về đường dẫn tương đối.

        ``resolver`` là hàm tra cứu của ``app``: nó kiểm tra ảnh có thuộc
        Workspace của người đang đăng nhập không, rồi trả về tệp thật.
        """
        path, name = resolver(filename)
        path = Path(path)
        relative = self._relative(path)
        if relative is None:
            # Ảnh nằm ngoài thư mục dữ liệu: chép vào thư mục upload của
            # Workspace để worker đọc được, và nhớ để dọn nếu thất bại.
            folder = self.uploads_root / str(self.pipeline_id)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / Path(name).name
            if not target.exists():
                shutil.copyfile(path, target)
                target.chmod(0o600)
                self.created.append(target)
            relative = f"uploads/{self.pipeline_id}/{target.name}"
        if relative not in self.paths:
            self.paths.append(relative)
        return relative

    def _relative(self, path: Path) -> str | None:
        try:
            return str(path.relative_to(self.data_root))
        except ValueError:
            return None

    def cleanup(self) -> None:
        """Dọn ảnh vừa tạo, dùng khi chiến dịch thất bại."""
        for path in self.created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        self.created = []

    def commit(self) -> None:
        """Giữ lại ảnh: chiến dịch đã tạo job thành công."""
        self.created = []
