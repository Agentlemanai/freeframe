"""The multipart UploadId must fit whatever the storage backend issues.

initiate stores the backend's UploadId on the version. The rest of the suite uses
short stand-ins like "upload-1", which is how a VARCHAR(255) column survived until
Cloudflare R2 -- whose IDs are 343 characters -- made every initiate fail with 500.
"""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from apps.api.models.asset import AssetVersion

# Length of the UploadIds Cloudflare R2 returned for keys of this app's shape
# (raw/<project>/<asset>/<version>/original.<ext>), measured 2026-09-19.
R2_UPLOAD_ID_LENGTH = 343


def test_upload_id_column_has_no_length_limit():
    column_type = AssetVersion.__table__.c.upload_id.type
    assert getattr(column_type, "length", None) is None, (
        f"upload_id is limited to {column_type.length} characters; "
        f"R2 issues {R2_UPLOAD_ID_LENGTH}"
    )


def test_migrations_have_a_single_head():
    # `alembic upgrade head` runs on every API start and refuses to pick between
    # two heads -- a mis-chained migration would stop the API from booting at all.
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"expected one migration head, found {heads}"
