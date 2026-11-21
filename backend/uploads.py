"""上传落盘：文件大小与扩展名的唯一校验点。

普通导入接口和账本助手上传共用这一份实现，避免两边限流/白名单各写一套后漂移。
调用方可以把上限作为参数传进来（主页面的测试会临时把它调小），默认值就是产品值。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, UploadFile

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {'.xlsx', '.csv', '.zip'}


def safe_name(filename: str | None) -> str:
    """只取 basename，绝不信任客户端传来的路径。"""
    return Path((filename or '').replace('\\', '/')).name


async def save_upload(file: UploadFile, target_dir: Path, *,
                      max_bytes: int | None = None,
                      allowed: set[str] | None = None) -> tuple[Path, str]:
    limit = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    extensions = ALLOWED_EXTENSIONS if allowed is None else allowed
    name = safe_name(file.filename)
    if not name or Path(name).suffix.lower() not in extensions:
        raise HTTPException(400, '请上传 .xlsx、.csv 或 .zip 格式的账单')

    target = target_dir / name
    size = 0
    with target.open('wb') as stream:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f'账单文件超过 {limit // (1024 * 1024)} MB 限制')
            stream.write(chunk)
    if size == 0:
        raise HTTPException(400, '上传文件为空')
    return target, name
