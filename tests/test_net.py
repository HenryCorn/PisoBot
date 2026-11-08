import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from unittest.mock import Mock, patch

import requests

from pisobot import net


def response(status=200, body="<html>Listing</html>", headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = body.encode()
    result.url = "https://portal.example/search"
    result.headers.update(headers or {})
    return result


class FetchChainTests(unittest.TestCase):
    def setUp(self):
        net._hosts.clear()
        env = patch.dict(os.environ, {"FETCH_CHAIN": "plain", "FETCH_MIN_GAP_S": "0", "DECODO_AUTH_TOKEN": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_unset_chain_preserves_legacy_retries_and_decodo_selection(self):
        del os.environ["FETCH_CHAIN"]
        with patch.object(net, "get", return_value=response(body="direct")) as get:
            self.assertEqual(net.page("https://portal.example", retries=7), "direct")
        get.assert_called_once_with("https://portal.example", params=None, retries=7)
        os.environ["DECODO_AUTH_TOKEN"] = "test-token"
        with patch.object(net.session, "post", return_value=Mock(
                status_code=200, json=lambda: {"results": [{"content": "proxied", "status_code": 200}]})) as post:
            self.assertEqual(net.page("https://portal.example"), "proxied")
        self.assertEqual(post.call_count, 1)

    def test_plain_ignores_token_and_does_not_retry(self):
        os.environ["DECODO_AUTH_TOKEN"] = "unused-test-token"
        with patch.object(net.session, "get", return_value=response()) as get, patch.object(net.session, "post") as post:
            self.assertIn("Listing", net.page("https://portal.example/search", {"beds": 1}, retries=9))
        get.assert_called_once_with("https://portal.example/search?beds=1",
                                    timeout=30, verify=net.VERIFY, allow_redirects=False)
        post.assert_not_called()

    def test_invalid_configuration_fails_before_network(self):
        for chain, gap, token in [("", "4", ""), ("browser", "4", ""), ("plain,plain", "4", ""),
                                  ("decodo,plain", "4", "test-token"), ("plain,decodo", "4", ""),
                                  ("plain", "-1", ""), ("plain", "nan", ""), ("plain", "inf", ""),
                                  ("plain", "wrong", ""), ("plain", "3601", "")]:
            with self.subTest(chain=chain, gap=gap), patch.dict(os.environ, {
                    "FETCH_CHAIN": chain, "FETCH_MIN_GAP_S": gap, "DECODO_AUTH_TOKEN": token}), \
                    patch.object(net.session, "get") as get, patch.object(net.session, "post") as post:
                with self.assertRaises(ValueError):
                    net.page("https://portal.example")
                get.assert_not_called()
                post.assert_not_called()

    def test_blocks_make_one_attempt_then_fail_fast_for_same_host(self):
        for status in (403, 429, 503):
            with self.subTest(status=status):
                net._hosts.clear()
                with patch.object(net.session, "get", return_value=response(status, headers={"Retry-After": "120"})) as get, \
                        patch.object(net.time, "monotonic", return_value=100):
                    with self.assertRaises(net.FetchBlocked):
                        net.page("https://portal.example/a", retries=5)
                    with self.assertRaises(net.FetchBlocked):
                        net.page("https://portal.example/b")
                    self.assertEqual(net._hosts["portal.example"].cooldown, 220)
                    self.assertEqual(get.call_count, 1)

    def test_retry_after_http_date_invalid_and_bound(self):
        with patch.object(net.time, "time", return_value=1000):
            self.assertEqual(net._retry_after(formatdate(1090, usegmt=True)), 90)
            self.assertEqual(net._retry_after("999999"), 3600)
            self.assertEqual(net._retry_after("invalid"), 300)
            self.assertEqual(net._retry_after(None), 300)
            self.assertEqual(net._retry_after("nan"), 300)

    def test_cooldown_expires(self):
        with patch.object(net.session, "get", side_effect=[response(429, headers={"Retry-After": "10"}), response()]) as get:
            with patch.object(net.time, "monotonic", return_value=100):
                with self.assertRaises(net.FetchBlocked):
                    net.page("https://portal.example")
            with patch.object(net.time, "monotonic", return_value=111):
                self.assertIn("Listing", net.page("https://portal.example"))
            self.assertEqual(get.call_count, 2)

    def test_fallthrough_only_for_blocks(self):
        os.environ.update(FETCH_CHAIN="plain,decodo", DECODO_AUTH_TOKEN="test-token")
        for body in ("", "<title>Just a moment...</title>", '<form id="challenge-form">Please wait</form>'):
            net._hosts.clear()
            with self.subTest(body=body), patch.object(net.session, "get", return_value=response(body=body)), \
                    patch.object(net, "_legacy_page", return_value="<html>real listing</html>") as decodo:
                self.assertIn("real listing", net.page("https://portal.example"))
                decodo.assert_called_once_with("https://portal.example", params=None, retries=1)
        for status in (404, 500, 502):
            net._hosts.clear()
            with self.subTest(status=status), patch.object(net.session, "get", return_value=response(status)) as get, \
                    patch.object(net, "_legacy_page") as decodo:
                with self.assertRaises(requests.HTTPError):
                    net.page("https://portal.example")
                self.assertEqual(get.call_count, 1)
                decodo.assert_not_called()
        with patch.object(net.session, "get", side_effect=requests.Timeout), patch.object(net, "_legacy_page") as decodo:
            with self.assertRaises(requests.Timeout):
                net.page("https://different.example")
            decodo.assert_not_called()

    def test_background_cloudflare_script_is_not_a_challenge(self):
        html = ('<html><title>Foxtons properties to rent</title><article>1 bed flat £1500</article>'
                '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></html>')
        with patch.object(net.session, "get", return_value=response(body=html)):
            self.assertEqual(net.page("https://foxtons.example"), html)

    def test_default_gap_has_jitter_and_redirects_are_paced(self):
        del os.environ["FETCH_MIN_GAP_S"]
        with patch.object(net.session, "get", side_effect=[
                response(302, headers={"Location": "/listing"}), response()]) as get, \
                patch.object(net.time, "monotonic", return_value=100), \
                patch.object(net.time, "sleep") as sleep, patch.object(net.random, "uniform", return_value=5) as jitter:
            net.page("https://portal.example/123")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(5)
        jitter.assert_called_with(2, 6)
        self.assertEqual(get.call_args.args[0], "https://portal.example/listing")

    def test_one_in_flight_per_host_but_not_global(self):
        entered, release, other_host = threading.Event(), threading.Event(), threading.Event()
        active = 0
        max_active = 0
        guard = threading.Lock()

        def request(url, **kwargs):
            nonlocal active, max_active
            if "other.example" in url:
                other_host.set()
                return response()
            with guard:
                active += 1
                max_active = max(max_active, active)
            entered.set()
            if not release.wait(5):
                raise AssertionError("request was not released")
            with guard:
                active -= 1
            return response()

        with patch.object(net.session, "get", side_effect=request), ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(net.page, "https://portal.example/1")
            self.assertTrue(entered.wait(2))
            second = executor.submit(net.page, "https://portal.example/2")
            third = executor.submit(net.page, "https://other.example/1")
            try:
                self.assertTrue(other_host.wait(2))
            finally:
                release.set()
            for future in (first, second, third):
                future.result(timeout=3)
        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
