"""
lab_config/topology.py
----------------------
Shared lab topology data model.

No external dependencies — no pynetbox, no yaml, no requests.
Every subpackage (netbox, clab, cml) imports from here.
Nothing here imports from them.

Multi-site model
----------------
A LabTopology contains one or more LabSite objects.
Each Device belongs to exactly one site by short name.
The builder namespaces site slugs as ``{lab-name}-{site-name}`` so
sites belonging to different lab versions never collide in NetBox's
global slug space.

    lab  = LabTopology("cisco-ospf-v1", ...)
    sites = [LabSite("dc-a"), LabSite("dc-b")]

    NetBox site slugs produced:
        cisco-ospf-v1-dc-a
        cisco-ospf-v1-dc-b

    Device names are not slugs in NetBox, so they need no prefix.
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
    IOSV          = "iosv"
    IOL           = "iol"
    IOLL2         = "iol-l2"
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
        try:
            return cls(nb_type)
        except ValueError:
            return cls.VIRTUAL


# ---------------------------------------------------------------------------
# LabSite
# ---------------------------------------------------------------------------

@dataclass
class LabSite:
    """
    A site within a lab topology.

    The ``name`` is a short label used in device definitions and displayed
    in NetBox.  The builder produces the full namespaced slug automatically:

        LabSite("dc-a")  inside  LabTopology("cisco-ospf-v1")
        → NetBox slug: ``cisco-ospf-v1-dc-a``
        → NetBox name: ``dc-a``

    Args:
        name:        Short site name, e.g. ``"dc-a"``, ``"wan"``, ``"core"``.
        description: Optional description stored in NetBox.
        tags:        NetBox tag names to apply.
    """
    name: str
    description: str = ""
    tags: List[str] = field(default_factory=list)

    _netbox_id: Optional[int] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Interface / Device / Link
# ---------------------------------------------------------------------------

@dataclass
class Interface:
    """
    A single interface on a device.

    Args:
        name:        Interface name as it appears on the device,
                     e.g. ``GigabitEthernet0/0``, ``Ethernet1``, ``eth0``.
        ip_address:  IPv4/IPv6 in CIDR notation, e.g. ``10.0.0.1/30``.
        description: Free-text description.
        iface_type:  NetBox interface type (default: ``1000base-t``).
        enabled:     Marked enabled in NetBox.
        tags:        NetBox tag names.
    """
    name: str
    ip_address: Optional[str] = None
    description: Optional[str] = None
    iface_type: InterfaceType = InterfaceType.GIG_COPPER
    enabled: bool = True
    tags: List[str] = field(default_factory=list)

    _netbox_id: Optional[int] = field(default=None, repr=False)
    _netbox_ip_id: Optional[int] = field(default=None, repr=False)


@dataclass
class Device:
    """
    A node in the lab.

    Args:
        name:          Device hostname. Must be unique within the lab.
        platform:      :class:`DevicePlatform` value.
        role:          :class:`DeviceRole` value.
        site:          Short site name matching a :class:`LabSite` in the topology.
        interfaces:    List of :class:`Interface` objects.
        mgmt_ip:       Management IP in CIDR notation. Creates a ``Management0``
                       interface in NetBox and sets it as the device primary IP.
        image:         Container image or CML node definition.
                       Stored in NetBox device ``comments`` for use by exporters.
        tags:          NetBox tag names.
        custom_fields: NetBox custom field values.
    """
    name: str
    platform: DevicePlatform
    role: DeviceRole
    site: str
    interfaces: List[Interface] = field(default_factory=list)
    mgmt_ip: Optional[str] = None
    image: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    custom_fields: Dict[str, Any] = field(default_factory=dict)

    _netbox_id: Optional[int] = field(default=None, repr=False)

    def get_interface(self, name: str) -> Optional[Interface]:
        return next((i for i in self.interfaces if i.name == name), None)


@dataclass
class Link:
    """
    A cable between two device interfaces.

    Cross-site links are fine — just reference devices by name regardless
    of which site they belong to.
    """
    device_a: str
    interface_a: str
    device_b: str
    interface_b: str
    description: Optional[str] = None
    cable_type: str = "cat6"

    _netbox_id: Optional[int] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# LabTopology
# ---------------------------------------------------------------------------

@dataclass
class LabTopology:
    """
    A complete lab topology.

    Maps to one NetBox Tenant (under the ``Labs`` Tenant Group) containing
    one or more Sites (under the ``Lab Sites`` Site Group).

    Site slugs in NetBox are namespaced as ``{lab.name}-{site.name}`` so
    that labs with the same internal site names never collide across versions.

    Management prefix
    -----------------
    One flat management prefix per lab.  All device management IPs should
    fall within this prefix.  Created in IPAM as ``is_pool=True``.
    """

    name: str
    description: str = ""
    sites: List[LabSite] = field(default_factory=list)
    devices: List[Device] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)
    mgmt_prefix: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    def add_site(self, site: LabSite) -> "LabTopology":
        self.sites.append(site)
        return self

    def add_device(self, device: Device) -> "LabTopology":
        self.devices.append(device)
        return self

    def add_link(self, link: Link) -> "LabTopology":
        self.links.append(link)
        return self

    def get_site(self, name: str) -> Optional[LabSite]:
        return next((s for s in self.sites if s.name == name), None)

    def get_device(self, name: str) -> Optional[Device]:
        return next((d for d in self.devices if d.name == name), None)

    def validate(self) -> List[str]:
        """Return a list of errors. Empty list means the topology is valid."""
        errors: List[str] = []
        site_names   = {s.name for s in self.sites}
        device_names = {d.name for d in self.devices}

        seen: set = set()
        for s in self.sites:
            if s.name in seen:
                errors.append(f"Duplicate site name: '{s.name}'.")
            seen.add(s.name)

        seen = set()
        for d in self.devices:
            if d.name in seen:
                errors.append(f"Duplicate device name: '{d.name}'.")
            seen.add(d.name)

        for d in self.devices:
            if d.site not in site_names:
                errors.append(
                    f"Device '{d.name}' references site '{d.site}' "
                    f"which is not declared in topology.sites."
                )

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
