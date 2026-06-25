import pytest
from praxis.db import connect

@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "praxis_test.db")
    yield conn
    conn.close()
