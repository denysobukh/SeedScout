import hashlib

import pytest

from tg_torrent_downloader.torrent import MAX_TORRENT_BYTES, InvalidTorrent, validate_torrent


def encode(value):
    """Synthetic test-only bencode encoder; never reads real torrent files."""
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if type(value) is int:
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(encode(key) + encode(value[key]) for key in sorted(value)) + b"e"
    raise TypeError("Unsupported fixture value")


def info(**updates):
    result = {b"name": b"test.bin", b"length": 5, b"piece length": 4, b"pieces": b"x" * 40}
    result.update({key.replace("_", " ").encode(): value for key, value in updates.items()})
    return result


def metainfo(value=None):
    return encode(
        {
            b"announce": b"https://tracker.invalid/private-passkey",
            b"info": info() if value is None else value,
        }
    )


def test_identity_uses_original_info_bytes_and_not_outer_metadata():
    value = info()
    torrent = validate_torrent(metainfo(value))
    assert torrent.info_hash == hashlib.sha1(encode(value), usedforsecurity=False).hexdigest()
    assert (torrent.name, torrent.total_size, torrent.file_count) == ("test.bin", 5, 1)
    other = validate_torrent(encode({b"comment": b"different", b"info": value}))
    assert torrent.info_hash == other.info_hash
    assert torrent.data == metainfo(value)
    assert "private-passkey" not in repr(torrent)


def test_multifile_torrent_and_legacy_names():
    value = info(name="Тест".encode("cp1251"))
    del value[b"length"]
    value[b"files"] = [
        {b"length": 2, b"path": [b"dir", b"a"]},
        {b"length": 3, b"path": [b"dir", b"b"]},
    ]
    torrent = validate_torrent(metainfo(value))
    assert (torrent.name, torrent.total_size, torrent.file_count) == ("Тест", 5, 2)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"<html>login</html>",
        b"i1e",
        b"le",
        b"d",
        b"degarbage",
        b"d4:info",
        b"d4:infoi-0ee",
        b"d4:infoi01ee",
        b"d4:infoi+1ee",
        b"d4:infoi1.0ee",
        b"d4:info01:xe",
        b"d4:info100:xe",
        b"d4:info0:e",
        b"d4:infolee",
        b"d4:infoi1e4:infoi2ee",
        b"d1:zi1e1:ai2ee",
        b"di1ei2ee",
        b"d1:ai" + b"1" * 65 + b"ee",
        b"l" * 66 + b"e" * 66,
    ],
)
def test_rejects_malformed_or_noncanonical_bencode(raw):
    with pytest.raises(InvalidTorrent):
        validate_torrent(raw)


def test_size_and_node_limits():
    with pytest.raises(InvalidTorrent, match="16 MiB"):
        validate_torrent(b"x" * (MAX_TORRENT_BYTES + 1))
    with pytest.raises(InvalidTorrent, match="parsing limits"):
        validate_torrent(b"l" + b"0:" * 200_001 + b"e")


@pytest.mark.parametrize(
    "changes",
    [
        {"name": b""},
        {"name": b"../escape"},
        {"name": b"C:escape"},
        {"name": b"bad\x00name"},
        {"length": -1},
        {"length": b"5"},
        {"length": 2**63},
        {"piece_length": 0},
        {"piece_length": b"4"},
        {"piece_length": 2**32},
        {"pieces": b"x" * 20},
        {"pieces": b"x" * 21},
        {"pieces": []},
        {"meta_version": 2},
    ],
)
def test_rejects_invalid_metainfo(changes):
    with pytest.raises(InvalidTorrent):
        validate_torrent(metainfo(info(**changes)))


def test_zero_length_file_has_no_piece_hashes():
    assert validate_torrent(metainfo(info(length=0, pieces=b""))).total_size == 0


@pytest.mark.parametrize(
    "files",
    [
        [],
        b"invalid",
        [1],
        [{b"length": 5, b"path": []}],
        [{b"length": 5, b"path": [b"..", b"escape"]}],
        [{b"length": 5, b"path": [b"dir/file"]}],
        [{b"length": 5, b"path": [b"file"], b"path.utf-8": [b".."]}],
        [{b"length": 5, b"path": [b"file"], b"symlink path": [b"other"]}],
        [{b"length": 2, b"path": [b"a"]}, {b"length": 3, b"path": [b"a"]}],
        [{b"length": 2, b"path": [b"a"]}, {b"length": 3, b"path": [b"a", b"b"]}],
    ],
)
def test_rejects_invalid_multifile_layouts(files):
    value = info()
    del value[b"length"]
    value[b"files"] = files
    with pytest.raises(InvalidTorrent):
        validate_torrent(metainfo(value))


def test_missing_info_and_conflicting_layouts():
    with pytest.raises(InvalidTorrent):
        validate_torrent(encode({b"comment": b"private-passkey"}))
    with pytest.raises(InvalidTorrent):
        validate_torrent(metainfo(info(files=[])))
    value = info()
    del value[b"length"]
    with pytest.raises(InvalidTorrent):
        validate_torrent(metainfo(value))


def test_validation_errors_do_not_include_tracker_passkeys():
    with pytest.raises(InvalidTorrent) as caught:
        validate_torrent(metainfo(info(length=-1)))
    assert "private-passkey" not in str(caught.value)
