"""Suite-wide guard against outbound network connections during tests.

Every test that touches an AI provider mocks it (fake clients or a scripted HTTP transport). This guard makes that a
rule instead of a convention: while the test package is loaded, a socket may only connect to loopback addresses or
UNIX sockets (the test database, Redis), so an unmocked provider call fails immediately instead of reaching a paid API.
Installed when the test package is imported and again from the before_tests hook (idempotent).
"""

import ipaddress
import socket

_installed = False


class OutboundNetworkBlocked(ConnectionError):
	"""An automated test tried to open a connection to a non-local address."""


def _allowed(address):
	if not isinstance(address, tuple):  # AF_UNIX path
		return True
	host = address[0]
	if host in ("localhost",):
		return True
	try:
		return ipaddress.ip_address(host.split("%")[0]).is_loopback
	except ValueError:
		return False  # an unresolved host name: never local in tests


def install():
	global _installed
	if _installed:
		return
	original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

	def connect(self, address):
		if not _allowed(address):
			raise OutboundNetworkBlocked(f"outbound connection blocked in tests: {address!r}")
		return original_connect(self, address)

	def connect_ex(self, address):
		if not _allowed(address):
			raise OutboundNetworkBlocked(f"outbound connection blocked in tests: {address!r}")
		return original_connect_ex(self, address)

	socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
	_installed = True


def installed():
	return _installed
