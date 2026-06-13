from app.modules.filemanager.storages.smb import SMB


def test_normalize_path_converts_separators_without_connection():
    """SMB 路径标准化应兼容 Python 3.11 并转换目录分隔符。"""
    storage = object.__new__(SMB)
    storage._server_path = "\\\\server\\share"

    assert storage._normalize_path("/movies/example.mkv") == (
        "\\\\server\\share\\movies\\example.mkv"
    )
