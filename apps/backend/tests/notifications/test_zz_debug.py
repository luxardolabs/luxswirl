import sys

import pytest

pytestmark = pytest.mark.integration


def test_show_sys_path():
    for i, p in enumerate(sys.path[:10]):
        print(f"[{i}] {p}")
    import notifications

    print("notifications:", notifications.__file__, notifications.__path__)
    try:
        import notifications.providers

        print("providers:", notifications.providers.__file__)
    except ImportError as e:
        print("providers import error:", e)
