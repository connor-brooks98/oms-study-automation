from sqlalchemy import func, select

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.models import UploadBatchModel
from tests.support import csrf_client


def test_invalid_multipart_file_rejects_entire_batch_before_staging(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    client = csrf_client(app)

    response = client.post(
        "/uploads/transcripts",
        files=[
            ("files", ("valid.txt", b"valid transcript", "text/plain")),
            ("files", ("empty.txt", b"", "text/plain")),
        ],
    )

    assert response.status_code == 422
    batches = tmp_path / "staging" / "batches"
    assert not batches.exists() or list(batches.iterdir()) == []
    with app.state.database.session() as session:
        assert session.scalar(select(func.count()).select_from(UploadBatchModel)) == 0
