"""Surface tests for the repo-root ``ezclassify`` dev-harness package.

The harness carries no implementation of its own: its public names must be
the very objects shipped in ``ezclassify-rest-client``, and its
``__version__`` must fall back to the pyproject literal when the package is
not installed.
"""

import importlib
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch


def test_entry_points_delegate_to_the_published_client():
    import ezclassify
    import ezclassify_rest_client

    assert ezclassify.EzClassifyClient is ezclassify_rest_client.EzClassifyClient
    assert ezclassify.connect_context is ezclassify_rest_client.connect_context


def test_all_names_resolve():
    import ezclassify

    for name in ezclassify.__all__:
        assert getattr(ezclassify, name) is not None


def test_version_falls_back_to_literal_when_package_not_installed():
    import ezclassify as _ez

    original = _ez.__version__

    with patch(
        "importlib.metadata.version", side_effect=PackageNotFoundError("ezclassify")
    ):
        importlib.reload(_ez)
        # The fallback literal applies when the package metadata is missing.
        assert _ez.__version__ == "1.0.0.dev0"

    # Restore so the rest of the test session sees the installed version
    importlib.reload(_ez)
    assert _ez.__version__ == original
