"""安全解析照片调取批次 ZIP。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import uuid
import zipfile
from io import BytesIO

from config import settings
from services.workflow_support import detect_attachment_mime


MAX_PHOTO_IMPORT_ZIP_BYTES = 200 * 1024 * 1024
MAX_PHOTO_IMPORT_FILES = 1000
MAX_PHOTO_IMPORT_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_PHOTO_IMPORT_SINGLE_BYTES = 20 * 1024 * 1024
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
_IDENTITY = re.compile(r"^(?:\d{15}|\d{17}[0-9X])$")
_GENERATED_PHOTO_NAME = re.compile(r"^(?:photo|照片)-\d+-[0-9a-f]{8}(?:\.[a-z0-9]+)?$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedPhoto:
    member_name: str
    safe_name: str
    legacy_safe_name: str
    person_name: str
    identity_number: str
    size_bytes: int
    sha256: str
    extension: str
    parse_error: str = ""


def normalize_identity(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def canonical_photo_filename(person_name: str, identity_number: str, extension: str) -> str:
    """生成照片附件对用户可见的规范名称，统一 JPEG 后缀为 .jpg。"""
    name = str(person_name or "").strip()
    identity = normalize_identity(identity_number)
    suffix = str(extension or ".jpg").lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    if suffix not in PHOTO_EXTENSIONS:
        suffix = ".jpg"
    return f"{name}_{identity}{suffix}"


def is_generated_photo_filename(value: str) -> bool:
    return bool(_GENERATED_PHOTO_NAME.fullmatch(str(value or "").strip()))


def repair_legacy_zip_text(value: str) -> str:
    """修复未标记 UTF-8、被 zipfile 按 CP437 解码的中文文件名。"""
    text = str(value or "")
    try:
        candidate = text.encode("cp437").decode("gb18030")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    original_cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    candidate_cjk = sum("\u3400" <= char <= "\u9fff" for char in candidate)
    return candidate if candidate_cjk > original_cjk else text


def decoded_zip_member_name(item: zipfile.ZipInfo) -> str:
    if item.flag_bits & 0x800:
        return item.filename
    return repair_legacy_zip_text(item.filename)


def _photo_member_parts(member_name: str) -> tuple[str, str]:
    name = str(member_name or "").replace("\\", "/")
    path = PurePosixPath(name)
    if (
        "\x00" in name
        or path.is_absolute()
        or PureWindowsPath(name).is_absolute()
        or PureWindowsPath(name).drive
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("文件路径无效")
    safe_name = path.name
    extension = Path(safe_name).suffix.lower()
    if extension not in PHOTO_EXTENSIONS:
        raise ValueError("仅支持 JPG、PNG、WebP 和 HEIC 照片")
    return safe_name, extension


def parse_photo_filename(member_name: str) -> tuple[str, str, str]:
    """返回安全文件名、姓名和标准化身份证号。"""
    safe_name, _ = _photo_member_parts(member_name)
    stem = Path(safe_name).stem
    if "_" not in stem:
        raise ValueError("文件名必须使用“姓名_身份证号”格式")
    person_name, identity_number = stem.rsplit("_", 1)
    person_name = person_name.strip()
    identity_number = normalize_identity(identity_number)
    if not person_name:
        raise ValueError("文件名缺少姓名")
    if not _IDENTITY.fullmatch(identity_number):
        raise ValueError("文件名中的身份证号格式无效")
    return safe_name, person_name, identity_number


def inspect_photo_zip(content: bytes) -> list[ParsedPhoto]:
    if not content or len(content) > MAX_PHOTO_IMPORT_ZIP_BYTES:
        raise ValueError("ZIP 文件不能超过 200MB")
    if not zipfile.is_zipfile(BytesIO(content)):
        raise ValueError("上传的文件不是有效 ZIP")
    parsed: list[ParsedPhoto] = []
    total_uncompressed = 0
    with zipfile.ZipFile(BytesIO(content)) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > MAX_PHOTO_IMPORT_FILES:
            raise ValueError("单个 ZIP 最多包含 1000 张照片")
        seen_safe_names: set[str] = set()
        for item in members:
            mode = (item.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("ZIP 内不允许包含软链接")
            if item.file_size > MAX_PHOTO_IMPORT_SINGLE_BYTES:
                raise ValueError("ZIP 内单张照片不能超过 20MB")
            if item.compress_size and item.file_size > item.compress_size * 200:
                raise ValueError("ZIP 压缩比异常，已拒绝读取")
            total_uncompressed += item.file_size
            if total_uncompressed > MAX_PHOTO_IMPORT_UNCOMPRESSED_BYTES:
                raise ValueError("ZIP 解压后的总大小不能超过 500MB")
            member_name = decoded_zip_member_name(item)
            safe_name, extension = _photo_member_parts(member_name)
            try:
                legacy_safe_name, _ = _photo_member_parts(item.filename)
            except ValueError:
                legacy_safe_name = safe_name
            if safe_name in seen_safe_names:
                raise ValueError("ZIP 内不允许包含同名照片")
            seen_safe_names.add(safe_name)
            parse_error = ""
            try:
                _, person_name, identity_number = parse_photo_filename(member_name)
            except ValueError as exc:
                # 文件路径和扩展名已经在上面严格校验；其余命名/身份证问题保留到
                # 批次明细中，便于基础管控一次性看到并处理，而不是拒绝整批 ZIP。
                parse_error = str(exc)
                stem = Path(safe_name).stem
                person_name = stem.rsplit("_", 1)[0].strip() if "_" in stem else stem.strip()
                identity_number = ""
            try:
                data = archive.read(item)
            except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
                raise ValueError("ZIP 内照片读取失败") from exc
            if len(data) != item.file_size:
                raise ValueError("ZIP 内照片大小校验失败")
            try:
                detect_attachment_mime(data, Path(safe_name).suffix.lower())
            except ValueError as exc:
                raise ValueError(f"{safe_name}：{exc}") from exc
            parsed.append(
                ParsedPhoto(
                    member_name=member_name,
                    safe_name=safe_name,
                    legacy_safe_name=legacy_safe_name,
                    person_name=person_name,
                    identity_number=identity_number,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    extension=extension,
                    parse_error=parse_error,
                )
            )
    if not parsed:
        raise ValueError("ZIP 内没有照片")
    return parsed


def read_photo_zip_members(content: bytes) -> dict[str, bytes]:
    """确认批次时重新读取照片，键使用安全文件名。"""
    result: dict[str, bytes] = {}
    with zipfile.ZipFile(BytesIO(content)) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            safe_name, _ = _photo_member_parts(decoded_zip_member_name(item))
            result[safe_name] = archive.read(item)
    return result


def save_photo_import_zip(batch_token: str, content: bytes) -> str:
    root = Path(settings.WORKFLOW_PHOTO_IMPORT_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    token = re.sub(r"[^0-9a-f-]", "", batch_token.lower()) or str(uuid.uuid4())
    target = (root / f"{token}.zip").resolve()
    if target.parent != root:
        raise ValueError("照片批次目录无效")
    temporary = root / f".{token}.partial"
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return f"{token}.zip"


def resolve_photo_import_zip(storage_key: str) -> Path:
    root = Path(settings.WORKFLOW_PHOTO_IMPORT_DIR).resolve()
    target = (root / storage_key).resolve()
    if root not in target.parents or not target.is_file() or target.is_symlink():
        raise FileNotFoundError("照片批次文件不存在")
    return target


def remove_photo_import_zip(storage_key: str) -> None:
    try:
        resolve_photo_import_zip(storage_key).unlink()
    except FileNotFoundError:
        pass
