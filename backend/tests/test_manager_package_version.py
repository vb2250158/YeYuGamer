import unittest

from yeyu_gamer_manager import __version__
from yeyu_gamer_manager.services.manager import ManagerService


class ManagerPackageVersionTest(unittest.TestCase):
    def test_api_version_uses_installed_package_version(self):
        self.assertEqual(ManagerService.VERSION, __version__)


if __name__ == '__main__':
    unittest.main()
