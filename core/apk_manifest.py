"""Minimal Android binary XML reader used to verify APK versionName values.

The vault sync workflow must never publish a Google Play latest artifact under
an older target version. Pulling a full APK parser into the runtime just for
that check would add a large dependency, so this module reads the binary
``AndroidManifest.xml`` chunk stream directly.

Only the small XML subset needed for ``manifest`` attributes is implemented.
Malformed or unusual manifests raise :class:`ManifestParseError` (or are
reported as ``None`` by :func:`read_version_name`) instead of guessing.
"""

import shutil
import struct
import tempfile
import zipfile

RES_XML_TYPE = 0x0003
RES_STRING_POOL_TYPE = 0x0001
RES_XML_START_ELEMENT_TYPE = 0x0102
STRING_POOL_UTF8_FLAG = 0x0100
NO_ENTRY = 0xFFFFFFFF
TYPE_STRING = 0x03
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_APK_MEMBER_BYTES = 1536 * 1024 * 1024
MAX_NESTED_APKS = 24
CHUNK_SIZE = 1024 * 1024
VERSION_ATTRIBUTE = "versionName"


class ManifestParseError(ValueError):
    """Raised when an AndroidManifest.xml chunk stream is malformed."""


def _u16(data: bytes, offset: int) -> int:
    """Reads a little-endian uint16 with bounds checking."""
    if offset < 0 or offset + 2 > len(data):
        raise ManifestParseError("truncated uint16")
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    """Reads a little-endian uint32 with bounds checking."""
    if offset < 0 or offset + 4 > len(data):
        raise ManifestParseError("truncated uint32")
    return struct.unpack_from("<I", data, offset)[0]


def _decode_utf8_length(data: bytes, offset: int) -> tuple[int, int]:
    """Decodes ResStringPool's one-or-two byte UTF-8 length prefix."""
    if offset >= len(data):
        raise ManifestParseError("truncated UTF-8 length")
    first = data[offset]
    if first & 0x80:
        if offset + 1 >= len(data):
            raise ManifestParseError("truncated UTF-8 length")
        return ((first & 0x7F) << 8) | data[offset + 1], offset + 2
    return first, offset + 1


def _parse_string_pool(
    data: bytes, offset: int, chunk_size: int
) -> list[str]:
    """Parses a ResStringPool chunk into a list of decoded strings."""
    if chunk_size < 28:
        raise ManifestParseError("string pool header too small")

    string_count, _style_count, flags, strings_start, _styles_start = (
        struct.unpack_from("<IIIII", data, offset + 8)
    )
    if string_count > 1_000_000:
        raise ManifestParseError("unreasonable string pool size")

    table_end = offset + 28 + (string_count * 4)
    if table_end > offset + chunk_size:
        raise ManifestParseError("string offset table exceeds chunk")

    pool_end = offset + chunk_size
    strings_base = offset + strings_start
    if strings_start and strings_base > pool_end:
        raise ManifestParseError("string data exceeds chunk")

    strings: list[str] = []
    for index in range(string_count):
        relative = _u32(data, offset + 28 + (index * 4))
        position = strings_base + relative
        if position >= pool_end:
            raise ManifestParseError("string offset outside chunk")

        if flags & STRING_POOL_UTF8_FLAG:
            _utf16_length, position = _decode_utf8_length(data, position)
            byte_length, position = _decode_utf8_length(data, position)
            end = position + byte_length
            if end > pool_end:
                raise ManifestParseError("UTF-8 string exceeds chunk")
            value = data[position:end].decode("utf-8", errors="replace")
        else:
            char_length = _u16(data, position)
            position += 2
            end = position + (char_length * 2)
            if end > pool_end:
                raise ManifestParseError("UTF-16 string exceeds chunk")
            value = data[position:end].decode("utf-16-le", errors="replace")

        strings.append(value)

    return strings


def _parse_start_element(
    data: bytes, offset: int, chunk_size: int, strings: list[str]
) -> str | None:
    """Returns the versionName attribute from a start-element chunk."""
    if chunk_size < 36:
        raise ManifestParseError("start element header too small")

    attribute_start = _u16(data, offset + 24)
    attribute_size = _u16(data, offset + 26)
    attribute_count = _u16(data, offset + 28)
    if attribute_size < 20:
        raise ManifestParseError("invalid attribute size")

    chunk_end = offset + chunk_size
    attribute_base = offset + 16 + attribute_start

    for index in range(attribute_count):
        attribute = attribute_base + (index * attribute_size)
        if attribute + 20 > chunk_end:
            raise ManifestParseError("attribute exceeds chunk")

        name_index = _u32(data, attribute + 4)
        if name_index >= len(strings):
            continue
        if strings[name_index] != VERSION_ATTRIBUTE:
            continue

        raw_index = _u32(data, attribute + 8)
        if raw_index != NO_ENTRY and raw_index < len(strings):
            value = strings[raw_index]
        else:
            data_type = data[attribute + 15]
            data_value = _u32(data, attribute + 16)
            if data_type != TYPE_STRING or data_value >= len(strings):
                continue
            value = strings[data_value]

        stripped = value.strip()
        if stripped:
            return stripped

    return None


def find_version_name(data: bytes) -> str | None:
    """Extracts versionName from an in-memory binary AndroidManifest.xml."""
    if len(data) < 8:
        raise ManifestParseError("manifest too small")

    xml_type, header_size, _total_size = struct.unpack_from("<HHI", data, 0)
    if xml_type != RES_XML_TYPE:
        raise ManifestParseError("not a binary XML document")
    if header_size < 8 or header_size > len(data):
        raise ManifestParseError("invalid XML header size")

    strings: list[str] = []
    offset = header_size
    while offset + 8 <= len(data):
        chunk_type, _chunk_header, chunk_size = struct.unpack_from(
            "<HHI", data, offset
        )
        if chunk_size < 8:
            raise ManifestParseError("invalid chunk size")
        if offset + chunk_size > len(data):
            raise ManifestParseError("chunk exceeds manifest")

        if chunk_type == RES_STRING_POOL_TYPE:
            strings = _parse_string_pool(data, offset, chunk_size)
        elif chunk_type == RES_XML_START_ELEMENT_TYPE and strings:
            value = _parse_start_element(data, offset, chunk_size, strings)
            if value:
                return value

        offset += chunk_size

    return None


def _read_manifest_member(archive: zipfile.ZipFile) -> bytes:
    """Reads the root AndroidManifest.xml with a strict size bound."""
    info = archive.getinfo("AndroidManifest.xml")
    if info.file_size > MAX_MANIFEST_BYTES:
        raise ManifestParseError("manifest exceeds size limit")
    with archive.open(info) as manifest:
        data = manifest.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestParseError("manifest exceeds size limit")
    return data


def _apk_candidates(names: list[str]) -> list[str]:
    """Orders nested APKs so the base package is inspected first."""
    apks = [
        name for name in names
        if name.lower().endswith(".apk") and not name.endswith("/")
    ]

    def rank(name: str) -> tuple[int, int, str]:
        base = name.rsplit("/", 1)[-1].lower()
        if base == "base.apk":
            return (0, len(name), name)
        if ".config." in base or base.startswith("split_config"):
            return (2, len(name), name)
        return (1, len(name), name)

    return sorted(apks, key=rank)


def _read_nested_version(
    archive: zipfile.ZipFile, member: str
) -> str | None:
    """Extracts versionName from an APK stored inside a split bundle."""
    info = archive.getinfo(member)
    if info.file_size > MAX_APK_MEMBER_BYTES:
        raise ManifestParseError("nested APK exceeds size limit")

    with tempfile.TemporaryFile(prefix="apk-manifest-") as nested:
        with archive.open(info) as source:
            shutil.copyfileobj(source, nested, length=CHUNK_SIZE)
        nested.seek(0)
        with zipfile.ZipFile(nested, "r") as inner:
            if "AndroidManifest.xml" not in inner.namelist():
                return None
            return find_version_name(_read_manifest_member(inner))


def read_version_name(path: str) -> str | None:
    """Returns the versionName stored in an APK, XAPK, APKM, or APKS file.

    ``None`` means the container was readable but no versionName could be
    confirmed. The caller must treat that as unverified and never relabel the
    artifact as a different version.
    """
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if "AndroidManifest.xml" in names:
                try:
                    value = find_version_name(_read_manifest_member(archive))
                except ManifestParseError:
                    value = None
                if value:
                    return value

            for member in _apk_candidates(names)[:MAX_NESTED_APKS]:
                try:
                    value = _read_nested_version(archive, member)
                except ManifestParseError:
                    continue
                if value:
                    return value
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
        return None
    return None
