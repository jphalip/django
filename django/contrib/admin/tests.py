import re
import base64
import httplib
import json
import os
import sys
import types
from unittest import SkipTest

from django.contrib.staticfiles.testing import StaticLiveServerCase
from django.utils.module_loading import import_by_path
from django.utils.translation import ugettext as _


class AdminSeleniumMetaClass(type):
    def __new__(cls, name, bases, attrs):
        """
        Dynamically injects new methods for running tests in different browsers
        when multiple browser specs are provided (e.g. --selenium=ff,gc).
        """
        all_specs = os.environ.get('DJANGO_SELENIUM_SPECS', '').split(',')
        if all_specs:
            for key, value in attrs.items():
                if isinstance(value, types.FunctionType) and key.startswith('test'):
                    func = value
                    for index, spec in enumerate(all_specs):
                        new_func_name = '%s__%s' % (key, spec.replace('/', '_'))
                        if index > 0:
                            new_func = types.FunctionType(func.func_code,
                                                          func.func_globals,
                                                          new_func_name,
                                                          func.func_defaults,
                                                          func.func_closure)
                            setattr(new_func, 'browser_spec', spec)
                            attrs[new_func_name] = new_func
                        else:
                            setattr(func, 'browser_spec', spec)
        return type.__new__(cls, name, bases, attrs)


class AdminSeleniumWebDriverTestCase(StaticLiveServerCase):
    __metaclass__ = AdminSeleniumMetaClass

    available_apps = [
        'django.contrib.admin',
        'django.contrib.auth',
        'django.contrib.contenttypes',
        'django.contrib.sessions',
        'django.contrib.sites',
    ]

    def _get_remote_capabilities(self, spec):
        """
        Returns the capabilities for the remote webdriver as specified in the
        given browser spec.
        """
        platforms = {
            'win8.1': 'Windows 8.1',
            'win8': 'Windows 8',
            'win7': 'Windows 7',
            'winxp': 'Windows XP',
            'linux': 'Linux',
            'mac10.9': 'Mac 10.9',
            'mac10.8': 'Mac 10.8',
            'mac10.6': 'Mac 10.6',
        }
        browsers = {
            'firefox': 'firefox',
            'opera': 'opera',
            'ie': 'internet explorer',
            'safari': 'safari',
            'ipad': 'ipad',
            'iphone': 'iphone',
            'android': 'android',
            'chrome': 'chrome'
        }

        version_pattern = re.compile('^v(\d*)$')
        version = None
        browser = 'firefox'
        platform = None
        bits = spec.split('/')
        for bit in bits:
            if bit.lower() in browsers:
                browser = browsers[bit.lower()]
            elif bit.lower() in platforms:
                platform = platforms[bit.lower()]
            else:
                version_match = version_pattern.match(bit)
                if version_match:
                    version = version_match.group(1)

        caps = {
            'browserName': browser,
            'version': version,
            'platform': platform,
            'public': 'public',
        }
        if 'BUILD_NUMBER' in os.environ:  # For Jenkins integration
            caps['build'] = os.environ['BUILD_NUMBER']
        elif 'TRAVIS_BUILD_NUMBER' in os.environ:  # For Travis integration
            caps['build'] = os.environ['TRAVIS_BUILD_NUMBER']
        return caps

    def _get_local_webdriver_class(self, spec):
        """
        Returns the webdriver for a local browser corresponding to the given
        browser spec.
        """
        browsers = {
            'firefox': 'selenium.webdriver.Firefox',
            'safari': 'selenium.webdriver.Safari',
            'opera': 'selenium.webdriver.Opera',
            'ie': 'selenium.webdriver.Ie',
            'chrome': 'selenium.webdriver.Chrome',
            'phantomjs': 'selenium.webdriver.PhantomJS',
        }
        return import_by_path(browsers[spec[:2]])

    def setUp(self):
        test_method = getattr(self, self._testMethodName)
        if not os.environ.get('DJANGO_SELENIUM_SPECS', ''):
            raise SkipTest('Selenium tests not requested')
        elif not hasattr(test_method, 'browser_spec'):
            raise SkipTest('Please make sure your test class is decorated with @browserize')

        browser_spec = test_method.browser_spec

        # Testing locally
        if not os.environ.get('DJANGO_SELENIUM_REMOTE', False):
            try:
                self.selenium = self._get_local_webdriver_class(browser_spec)()
            except Exception as e:
                raise SkipTest(
                    'Selenium specifications "%s" not valid or '
                    'corresponding WebDriver not installed: %s'
                    % (browser_spec, str(e)))

        # Testing remotely
        else:
            if not (os.environ.get('REMOTE_USER') and os.environ.get('REMOTE_KEY')):
                raise self.failureException('Both REMOTE_USER and REMOTE_KEY environment variables are required for remote tests.')

            from selenium.webdriver import Remote
            capabilities = self._get_remote_capabilities(browser_spec)
            capabilities['name'] = self.id()
            auth = '%(REMOTE_USER)s:%(REMOTE_KEY)s' % os.environ
            hub = os.environ.get('REMOTE_HUB', 'ondemand.saucelabs.com:80')
            self.selenium = Remote(
                command_executor='http://%s@%s/wd/hub' % (auth, hub),
                desired_capabilities=capabilities)

    def tearDown(self):
        if hasattr(self, 'selenium'):
            from selenium.webdriver import Remote
            if isinstance(self.selenium, Remote):
                self._report_sauce_pass_fail()
            self.selenium.quit()
        super(AdminSeleniumWebDriverTestCase, self).tearDown()

    def _report_sauce_pass_fail(self):
        # Sauce Labs has no way of knowing if the test passed or failed, so we
        # let it know.
        base64string = base64.encodestring(
            '%s:%s' % (os.environ.get('REMOTE_USER'), os.environ.get('REMOTE_KEY')))[:-1]
        result = json.dumps({'passed': sys.exc_info() == (None, None, None)})
        url = '/rest/v1/%s/jobs/%s' % (os.environ.get('REMOTE_USER'), self.selenium.session_id)
        connection = httplib.HTTPConnection('saucelabs.com')
        connection.request(
            'PUT', url, result, headers={"Authorization": 'Basic %s' % base64string})
        result = connection.getresponse()
        return result.status == 200

    def wait_until(self, callback, timeout=10):
        """
        Helper function that blocks the execution of the tests until the
        specified callback returns a value that is not falsy. This function can
        be called, for example, after clicking a link or submitting a form.
        See the other public methods that call this function for more details.
        """
        from selenium.webdriver.support.wait import WebDriverWait
        WebDriverWait(self.selenium, timeout).until(callback)

    def wait_loaded_tag(self, tag_name, timeout=10):
        """
        Helper function that blocks until the element with the given tag name
        is found on the page.
        """
        self.wait_for(tag_name, timeout)

    def wait_for(self, css_selector, timeout=10):
        """
        Helper function that blocks until an css selector is found on the page.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as ec
        self.wait_until(
            ec.presence_of_element_located((By.CSS_SELECTOR, css_selector)),
            timeout
        )

    def wait_for_text(self, css_selector, text, timeout=10):
        """
        Helper function that blocks until the text is found in the css selector.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as ec
        self.wait_until(
            ec.text_to_be_present_in_element(
                (By.CSS_SELECTOR, css_selector), text),
            timeout
        )

    def wait_for_value(self, css_selector, text, timeout=10):
        """
        Helper function that blocks until the value is found in the css selector.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as ec
        self.wait_until(
            ec.text_to_be_present_in_element_value(
                (By.CSS_SELECTOR, css_selector), text),
            timeout
        )

    def wait_page_loaded(self):
        """
        Block until page has started to load.
        """
        from selenium.common.exceptions import TimeoutException
        try:
            # Wait for the next page to be loaded
            self.wait_loaded_tag('body')
        except TimeoutException:
            # IE7 occasionnally returns an error "Internet Explorer cannot
            # display the webpage" and doesn't load the next page. We just
            # ignore it.
            pass

    def admin_login(self, username, password, login_url='/admin/'):
        """
        Helper function to log into the admin.
        """
        self.selenium.get('%s%s' % (self.live_server_url, login_url))
        username_input = self.selenium.find_element_by_name('username')
        username_input.send_keys(username)
        password_input = self.selenium.find_element_by_name('password')
        password_input.send_keys(password)
        login_text = _('Log in')
        self.selenium.find_element_by_xpath(
            '//input[@value="%s"]' % login_text).click()
        self.wait_page_loaded()

    def get_css_value(self, selector, attribute):
        """
        Helper function that returns the value for the CSS attribute of an
        DOM element specified by the given selector. Uses the jQuery that ships
        with Django.
        """
        return self.selenium.execute_script(
            'return django.jQuery("%s").css("%s")' % (selector, attribute))

    def get_select_option(self, selector, value):
        """
        Returns the <OPTION> with the value `value` inside the <SELECT> widget
        identified by the CSS selector `selector`.
        """
        from selenium.common.exceptions import NoSuchElementException
        options = self.selenium.find_elements_by_css_selector('%s > option' % selector)
        for option in options:
            if option.get_attribute('value') == value:
                return option
        raise NoSuchElementException('Option "%s" not found in "%s"' % (value, selector))

    def assertSelectOptions(self, selector, values):
        """
        Asserts that the <SELECT> widget identified by `selector` has the
        options with the given `values`.
        """
        options = self.selenium.find_elements_by_css_selector('%s > option' % selector)
        actual_values = []
        for option in options:
            actual_values.append(option.get_attribute('value'))
        self.assertEqual(values, actual_values)

    def has_css_class(self, selector, klass):
        """
        Returns True if the element identified by `selector` has the CSS class
        `klass`.
        """
        return (self.selenium.find_element_by_css_selector(selector)
                .get_attribute('class').find(klass) != -1)
