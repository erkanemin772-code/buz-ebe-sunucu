import argparse
import unittest
from relay import tls_context


class DeploymentTests(unittest.TestCase):
    def args(self, **changes):
        values = dict(bind="0.0.0.0", cert=None, key=None, dev_loopback=False,
                      behind_proxy=False, websocket=True)
        values.update(changes)
        return argparse.Namespace(**values)

    def test_public_listener_still_requires_tls(self):
        with self.assertRaises(SystemExit):
            tls_context(self.args())

    def test_loopback_flag_cannot_expose_plain_public_port(self):
        with self.assertRaises(SystemExit):
            tls_context(self.args(dev_loopback=True))

    def test_managed_proxy_accepts_only_explicit_websocket_mode(self):
        self.assertIsNone(tls_context(self.args(behind_proxy=True)))
        for changes in (dict(websocket=False), dict(dev_loopback=True),
                        dict(cert="certificate.pem"), dict(key="private.pem")):
            with self.assertRaises(SystemExit):
                tls_context(self.args(behind_proxy=True, **changes))


if __name__ == "__main__":
    unittest.main()
