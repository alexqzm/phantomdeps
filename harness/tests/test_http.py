"""http_get: a 404 whose body stalls (npm's error pages sometimes do) is still a 404, not an
exception that ends a classification."""

import socket
import threading
import time
import unittest

from phantomdeps.util import http_get


class StalledErrorBody(unittest.TestCase):
    def setUp(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.stop = False

        def serve():
            while not self.stop:
                try:
                    c, _ = self.srv.accept()
                except OSError:
                    return
                c.recv(4096)
                c.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\n"
                          b"Content-Length: 100\r\n\r\n{\"error\":")      # body never completes
                time.sleep(3)
                c.close()
        threading.Thread(target=serve, daemon=True).start()

    def tearDown(self):
        self.stop = True
        self.srv.close()

    def test_stalled_404_body(self):
        r = http_get(f"http://127.0.0.1:{self.port}/x", timeout=0.5, retries=1, backoff=0)
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
