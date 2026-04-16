"""
lab_tools/topology.py
---------------------
Shared lab topology data model.

No external dependencies — no pynetbox, no yaml, no requests.
Every subpackage (netbox, clab, cml) imports from here.
Nothing here imports from them.

Data flow:

    Python definition  ──►  NetBoxLabBuilder  ──►  NetBox (source of truth)
                                                          │
                                                    NetBoxLabReader
                                                          │
                                                          ▼
                                                    LabTopology  ──►  ClabExporter
                                                                  ──►  CmlExporter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class DevicePlatform(str, Enum):
    # Arista
    CEOS          = "ceos"
    VEOS          = "veos"
    # Cisco IOS / IOS-XE
    IOL           = "iol"
    IOLL2         = "iol-l2"
    IOSV          = "iosv"
    CSR1000V      = "csr1000v"
    CAT8000V      = "cat8000v"
    # Cisco IOS-XR
    XRVM          = "xrvm"
    XRD           = "xrd"
    # Cisco NX-OS
    NXOSV         = "nxosv"
    # Juniper
    VJUNOSEVOLVED = "vjunosevolved"
    VQFX          = "vqfx"
    VMXVCP        = "vmx-vcp"
    # Open-source / Linux
    LINUX         = "linux"
    VYOS          = "vyos"
    FRR           = "frr"
    OPENWRT       = "openwrt"

    @classmethod
    def from_slug(cls, slug: str) -> "DevicePlatform":
        """Reconstruct from a NetBox platform slug. Falls back to LINUX."""
        try:
            return cls(slug)
        except ValueError:
            return cls.LINUX


class DeviceRole(str, Enum):
    ROUTER   = "router"
    SWITCH   = "switch"
    FIREWALL = "firewall"
    SERVER   = "server"
    HOST     = "host"

    @classmethod
    def from_slug(cls, slug: str) -> "DeviceRole":
        """Reconstruct from a NetBox device-role slug. Falls back to HOST."""
        try:
            return cls(slug)
        except ValueError:
            return cls.HOST


class InterfaceType(str, Enum):
    VIRTUAL     = "virtual"
    GIG_COPPER  = "1000base-t"
    GIG_SFP     = "1000base-x-sfp"
    TEN_GIG_SFP = "10gbase-x-sfpp"
    HUNDRED_GIG = "100gbase-x-qsfp28"

    @classmethod
    def from_netbox(cls, nb_type: str) -> "InterfaceType":
        """Reconstruct from a NetBox interface type value. Falls back to VIRTUAL."""
        try:
            return cls(nb_type)
        except ValueError:
            return cls.VIRTUAL


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Interface:
    name: str
    ip_address: Optional[str] = None
    """IPv4/IPv6 in CIDR notation, e.g. ``10.0.0.1/30``."""

    description: Optional[str] = None
    iface_type: InterfaceType = InterfaceType.GIG_COPPER
    enabled: bool = True
    tags: List[str] = field(default_factory=list)

    # Populated by builder/reader — do not set manually
    _netbox_id: Optional[int] = field(default=None, repr=False)
    _netbox_ip_id: Optional[int] = field(default=None, repr=False)


@dataclass
class Device:
    name: str
    platform: DevicePlatform
    role: DeviceRole
    interfaces: List[Interface] = field(default_factory=list)

    mgmt_ip: Optional[str] = None
    """Management IP in CIDR notation. Stored on a Management0 interface in NetBox."""

    image: Optional[str] = None
    """Container image or CML node definition. Stored in NetBox device comments."""

    tags: List[str] = field(default_factory=list)
    custom_fields: Dict[str, Any] = field(default_factory=dict)

    # Populated by builder/reader — do not set manually
    _netbox_id: Optional[int] = field(default=None, repr=False)

    def get_interface(self, name: str) -> Optional[Interface]:
        return next((i for i in self.interfaces if i.name == name), None)


@dataclass
class Link:
    device_a: str
    interface_a: str
    device_b: str
    interface_b: str
    description: Optional[str] = None
    cable_type: str = "cat6"

    # Populated by builder/reader — do not set manually
    _netbox_id: Optional[int] = field(default=None, repr=False)


@dataclass
class LabTopology:
    """
    A complete lab topology.

    Maps 1:1 with a NetBox Tenant (under the ``Labs`` Tenant Group)
    and a Site (under the ``Lab Sites`` Site Group).
    """
    name: str
    description: str = ""
    devices: List[Device] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)

    mgmt_prefix: Optional[str] = None
    site_name: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    def add_device(self, device: Device) -> "LabTopology":
        self.devices.append(device)
        return self

    def add_link(self, link: Link) -> "LabTopology":
        self.links.append(link)
        return self

    def get_device(self, name: str) -> Optional[Device]:
        return next((d for d in self.devices if d.name == name), None)

    def validate(self) -> List[str]:
        """Return a list of errors. Empty list means the topology is valid."""
        errors: List[str] = []
        device_names = {d.name for d in self.devices}

        seen: set = set()
        for d in self.devices:
            if d.name in seen:
                errors.append(f"Duplicate device name: '{d.name}'.")
            seen.add(d.name)

        for link in self.links:
            for side, dev_name, iface_name in [
                ("A", link.device_a, link.interface_a),
                ("B", link.device_b, link.interface_b),
            ]:
                if dev_name not in device_names:
                    errors.append(
                        f"Link ({link.device_a}:{link.interface_a} ↔ "
                        f"{link.device_b}:{link.interface_b}) "
                        f"side {side}: device '{dev_name}' not in topology."
                    )
                    continue
                dev = self.get_device(dev_name)
                if not dev.get_interface(iface_name):
                    errors.append(
                        f"Link ({link.device_a}:{link.interface_a} ↔ "
                        f"{link.device_b}:{link.interface_b}) "
                        f"side {side}: interface '{iface_name}' not defined on '{dev_name}'."
                    )
        return errors
