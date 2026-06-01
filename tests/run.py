"""核心回归集入口：以 pytest 跑一组核心测试文件，命令行参数透传给 pytest。"""
import sys

import pytest

CORE = [
    "tests/test_metainfo.py",
    "tests/test_object.py",
    "tests/test_bluray.py",
    "tests/test_mediascrape.py",
    "tests/test_subscribe_chain.py",
    "tests/test_worker_schemas.py",
    "tests/test_worker_config.py",
    "tests/test_worker_client.py",
    "tests/test_worker_client_manager.py",
    "tests/test_worker_callback.py",
    "tests/test_worker_integration.py",
    "tests/test_system_utils_worker.py",
    "tests/test_indexer_worker.py",
]

if __name__ == "__main__":
    sys.exit(pytest.main(CORE + sys.argv[1:]))
