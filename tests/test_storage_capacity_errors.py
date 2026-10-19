"""Only real filesystem capacity errno values are classified as retryable 507."""
import errno
import pytest
from app.utils.file_io import is_storage_capacity_error


@pytest.mark.parametrize("error,expected", [
    (OSError(errno.ENOSPC, "full"), True),
    (OSError(getattr(errno, "EDQUOT", errno.ENOSPC), "quota"), True),
    (PermissionError(errno.EACCES, "denied"), False),
    (OSError(errno.EIO, "I/O fault"), False),
    (OSError("No space left on device"), False),
    (RuntimeError("No space left on device"), False),
])
def test_storage_capacity_classification_uses_errno(error, expected):
    assert is_storage_capacity_error(error) is expected
