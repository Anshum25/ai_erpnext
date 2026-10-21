# Importing any test module loads this package first: no test may reach the network (see network_guard.py).
from ai_erpnext.tests import network_guard as _network_guard

_network_guard.install()
