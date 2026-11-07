import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heibox.models import Post  # noqa: E402
from heibox.store import Store  # noqa: E402

USERID = 7


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "test.db")
    instance.ensure_user(USERID)
    yield instance
    instance.close()


@pytest.fixture
def post_factory():
    def make(linkid: int, ts: int, **kwargs) -> Post:
        kwargs.setdefault("title", f"post-{linkid}")
        return Post(linkid=linkid, userid=USERID, create_at=ts, **kwargs)

    return make
