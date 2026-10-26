"""__version__ falls back to a literal when package metadata is unavailable.

When the distribution is not pip-installed (so ``importlib.metadata.version``
raises ``PackageNotFoundError``), ``ezclassify_rest_client.__version__`` must
still resolve to the pinned literal rather than blowing up at import time.
"""

from __future__ import annotations

import importlib
import importlib.metadata

from unittest.mock import patch

import ezclassify_rest_client as _ec


class TestVersionFallback:
    def test_package_not_found_falls_back_to_literal(self):
        original = _ec.__version__
        with patch(
            "importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError(
                "ezclassify-rest-client"
            ),
        ):
            importlib.reload(_ec)
            assert _ec.__version__ == "1.0.0.dev0"
        importlib.reload(_ec)
        assert _ec.__version__ == original
