"""
lab_tools/loader.py
-------------------
Loads a LabTopology from a YAML file.

The YAML schema mirrors the dataclasses directly — no translation layer,
no magic keys.  The loader validates the topology after parsing and raises
clearly if anything is wrong before touching NetBox.

Usage::

    from lab_tools.loader import load_yaml
    from lab_tools.netbox import connect, NetBoxLabBuilder

    lab = load_yaml("labs/cisco-ospf-v1.yaml")
    NetBoxLabBuilder(connect()).build(lab)

YAML schema
-----------

    name: cisco-ospf-v1
    description: Multi-area OSPF lab
    mgmt_prefix: 192.168.100.0/24
    tags:
      - ospf
      - cisco

    sites:
      - name: dc-a
        description: Area 0 backbone
      - name: dc-b
        description: Area 1 edge

    devices:
      - name: r1
        platform: iol
        role: router
        site: dc-a
        mgmt_ip: 192.168.100.11/24
        image: iol:17.12          # optional — stored in NetBox comments
        interfaces:
          - name: Ethernet0/0
            ip_address: 10.0.12.1/30
            description: r1→r2
          - name: Loopback0
            ip_address: 10.255.0.1/32
            type: virtual
            description: Router-ID

    links:
      - a: r1:Ethernet0/0
        b: r2:Ethernet0/0
        description: Area 0 backbone
        cable_type: cat6          # optional, default cat6

Field reference
---------------
devices[].platform  : any DevicePlatform value (iol, iol-l2, iosv, ceos, ...)
devices[].role      : router | switch | firewall | server | host
interfaces[].type   : virtual | 1000base-t | 1000base-x-sfp | 10gbase-x-sfpp | 100gbase-x-qsfp28
                      default: 1000base-t
links[].a / .b      : device:interface  (colon-separated)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

import yaml

from .topology import (
    Device,
    DevicePlatform,
    DeviceRole,
    Interface,
    InterfaceType,
    LabSite,
    LabTopology,
    Link,
)

log = logging.getLogger(__name__)


def load_yaml(path: Union[str, Path]) -> LabTopology:
    """
    Parse *path* and return a validated :class:`~lab_tools.topology.LabTopology`.

    Raises:
        FileNotFoundError: If the file does not exist.
        yaml.YAMLError:    If the file is not valid YAML.
        ValueError:        If required fields are missing or the topology
                           fails validation.
    """
    path = Path(path)
    log.info("Loading topology from %s", path)

    with path.open("r") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse as a YAML mapping.")

    lab = _parse_topology(data)

    errors = lab.validate()
    if errors:
        raise ValueError(
            f"Topology '{lab.name}' loaded from {path} has validation errors:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )

    log.info(
        "Loaded lab '%s': %d site(s), %d device(s), %d link(s)",
        lab.name, len(lab.sites), len(lab.devices), len(lab.links),
    )
    return lab


# ---------------------------------------------------------------------------
# Internal parsers
# ---------------------------------------------------------------------------

def _require(data: dict, key: str, context: str) -> object:
    if key not in data:
        raise ValueError(f"Missing required field '{key}' in {context}.")
    return data[key]


def _parse_topology(data: dict) -> LabTopology:
    lab = LabTopology(
        name=str(_require(data, "name", "topology")),
        description=str(data.get("description", "")),
        mgmt_prefix=data.get("mgmt_prefix"),
        tags=list(data.get("tags", [])),
    )

    for site_data in data.get("sites", []):
        lab.add_site(_parse_site(site_data))

    for device_data in data.get("devices", []):
        lab.add_device(_parse_device(device_data))

    for link_data in data.get("links", []):
        lab.add_link(_parse_link(link_data))

    return lab


def _parse_site(data: dict) -> LabSite:
    return LabSite(
        name=str(_require(data, "name", "site")),
        description=str(data.get("description", "")),
        tags=list(data.get("tags", [])),
    )


def _parse_device(data: dict) -> Device:
    name = str(_require(data, "name", "device"))

    platform_raw = str(_require(data, "platform", f"device '{name}'")).lower()
    try:
        platform = DevicePlatform(platform_raw)
    except ValueError:
        valid = ", ".join(p.value for p in DevicePlatform)
        raise ValueError(
            f"Device '{name}': unknown platform '{platform_raw}'. Valid values: {valid}"
        )

    role_raw = str(_require(data, "role", f"device '{name}'")).lower()
    try:
        role = DeviceRole(role_raw)
    except ValueError:
        valid = ", ".join(r.value for r in DeviceRole)
        raise ValueError(
            f"Device '{name}': unknown role '{role_raw}'. Valid values: {valid}"
        )

    interfaces = [
        _parse_interface(iface_data, name)
        for iface_data in data.get("interfaces", [])
    ]

    return Device(
        name=name,
        platform=platform,
        role=role,
        site=str(_require(data, "site", f"device '{name}'")),
        interfaces=interfaces,
        mgmt_ip=data.get("mgmt_ip"),
        image=data.get("image"),
        tags=list(data.get("tags", [])),
        custom_fields=dict(data.get("custom_fields", {})),
    )


def _parse_interface(data: dict, device_name: str) -> Interface:
    name = str(_require(data, "name", f"interface on device '{device_name}'"))

    type_raw = str(data.get("type", "1000base-t")).lower()
    try:
        iface_type = InterfaceType(type_raw)
    except ValueError:
        valid = ", ".join(t.value for t in InterfaceType)
        raise ValueError(
            f"Device '{device_name}', interface '{name}': "
            f"unknown type '{type_raw}'. Valid values: {valid}"
        )

    return Interface(
        name=name,
        ip_address=data.get("ip_address"),
        description=data.get("description"),
        iface_type=iface_type,
        enabled=bool(data.get("enabled", True)),
        tags=list(data.get("tags", [])),
    )


def _parse_link(data: dict) -> Link:
    """
    Parse a link where endpoints are written as ``device:interface``.

        a: r1:Ethernet0/0
        b: r2:Ethernet0/0
    """
    def _split(side: str, value: str):
        parts = value.split(":", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Link side '{side}' must be in 'device:interface' format, got '{value}'."
            )
        return parts[0].strip(), parts[1].strip()

    a_raw = str(_require(data, "a", "link"))
    b_raw = str(_require(data, "b", "link"))

    dev_a, iface_a = _split("a", a_raw)
    dev_b, iface_b = _split("b", b_raw)

    return Link(
        device_a=dev_a,
        interface_a=iface_a,
        device_b=dev_b,
        interface_b=iface_b,
        description=data.get("description"),
        cable_type=str(data.get("cable_type", "cat6")),
    )
