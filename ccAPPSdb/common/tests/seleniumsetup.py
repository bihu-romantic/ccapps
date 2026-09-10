import os
import time
from selenium import webdriver

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from ccAPPSdb.common.models import User
from ccAPPSdb.common.utils import get_databases

noSelenium = settings.SELENIUM_TESTS is None


class SeleniumTest(StaticLiveServerTestCase):
    def setUp(self):
        # Login
        if not User.objects.filter(username="admin").count():
            User.objects.create_superuser("admin", "your@company.com", "admin")
        super().setUp()

    @classmethod
    def setUpClass(cls):
        for db in get_databases():
            # Avoid leaking persistent connections
            get_databases()[db]["CONN_MAX_AGE"] = 0
        os.environ["CCAPPS_TEST"] = "YES"
        super().setUpClass()
        if settings.SELENIUM_TESTS == "firefox":
            firefox_options = webdriver.FirefoxOptions()
            if settings.SELENIUM_HEADLESS:
                firefox_options.add_argument("--headless")
            cls.driver = webdriver.Firefox(options=firefox_options)
        elif settings.SELENIUM_TESTS == "chrome":
            import chromedriver_autoinstaller

            chromedriver_autoinstaller.install()
            options = webdriver.ChromeOptions()
            options.add_argument("--silent")
            if settings.SELENIUM_HEADLESS:
                options.add_argument("--headless")
            cls.driver = webdriver.Chrome(options=options)
        elif settings.SELENIUM_TESTS == "edge":
            from msedge.selenium_tools import Edge, EdgeOptions

            options = EdgeOptions()
            options.use_chromium = True
            options.add_argument("--silent")
            if settings.SELENIUM_HEADLESS:
                options.add_argument("--headless")
            cls.driver = Edge(options=options)
        else:
            raise Exception("Invalid setting SELENIUM_TESTS")
        cls.driver.set_window_size(1920, 1480)
        cls.driver.implicitly_wait(10)

    @classmethod
    def tearDownClass(cls):
        browser_log = cls.driver.get_log("browser")
        if browser_log:
            print("Browser console:")
            for l in browser_log:
                print(l["message"])
        cls.driver.quit()
        time.sleep(2)
        del os.environ["CCAPPS_TEST"]
        super().tearDownClass()

    @classmethod
    def saveScreenshot(cls, name="screenshot.png"):
        cls.driver.save_screenshot(name)
