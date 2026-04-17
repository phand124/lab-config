"""
lab_config/netbox
----------------
NetBox integration for lab_config.

Exports
-------
connect()           Convenience function — returns a configured pynetbox.api instance.
NetBoxLabBuilder    Push a LabTopology into NetBox.
NetBoxLabReader     Pull a LabTopology back out of NetBox.

Example::

    from lab_config.netbox import connect, NetBoxLabBuilder, NetBoxLabReader

    nb      = connect("http://netbox.lab:8000", token="abc123", verify_ssl=False)
    builder = NetBoxLabBuilder(nb)
    reader  = NetBoxLabReader(nb)

    # Push
    builder.build(my_lab)

    # Pull back later (NetBox is source of truth)
    lab = reader.get_lab("my-lab")
"""

import urllib3
import pynetbox

from .builder import NetBoxLabBuilder
from .reader import NetBoxLabReader

__all__ = [
    "connect",
    "NetBoxLabBuilder",
    "NetBoxLabReader",
]


def connect(base_url: str = None, token: str = None, verify_ssl: bool = None) -> pynetbox.api:
    from ..config import require_netbox_config
    url, tok, ssl = require_netbox_config()
    nb = pynetbox.api(base_url or url, token=token or tok)
    verify = verify_ssl if verify_ssl is not None else ssl
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        nb.http_session.verify = False
    return nb
