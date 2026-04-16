"""
lab_tools/netbox/reader.py
--------------------------
Reconstructs a LabTopology from NetBox.

This is what makes NetBox the actual source of truth — you don't need to
maintain a separate Python definition once a lab is in NetBox.  The reader
pulls devices, interfaces, IPs, and cables for a given tenant and rebuilds
the dataclass objects so any exporter (clab, cml) can consume them without
ever talking to NetBox directly.

Lookup path
-----------
tenant (slug = lab name)
  └── site (same slug)
        ├── devices
        │     ├── platform  → DevicePlatform
        │     ├── role      → DeviceRole
        │     ├── interfaces
        │     │     └── ip_addresses → Interface.ip_address
        │     └── primary_ip4 → Device.mgmt_ip
        └── cables (via device endpoints)
              └── Link(device_a, interface_a, device_b, interface_b)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import pynetbox

from ..topology import (
    Device,
    DevicePlatform,
    DeviceRole,
    Interface,
    InterfaceType,
    LabTopology,
    Link,
)

log = logging.getLogger(__name__)

# Management interface names we treat as mgmt_ip, not regular interfaces
_MGMT_IFACE_NAMES = {"Management0", "Management1", "mgmt0", "mgmt", "Mgmt0"}


class NetBoxLabReader:
    """
    Read a lab back out of NetBox as a :class:`~lab_tools.topology.LabTopology`.

    Args:
        nb: Connected ``pynetbox.api`` instance.

    Example::

        from lab_tools.netbox import connect, NetBoxLabReader

        nb     = connect("http://netbox.lab:8000", token="abc123", verify_ssl=False)
        reader = NetBoxLabReader(nb)

        lab = reader.get_lab("ebgp-triangle-01")
        print(lab.devices)
    """

    def __init__(self, nb: pynetbox.api) -> None:
        self.nb = nb

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_lab(self, name: str) -> LabTopology:
        """
        Reconstruct a :class:`~lab_tools.topology.LabTopology` by lab name.

        Args:
            name: Lab name (matches the NetBox tenant name/slug).

        Raises:
            ValueError: If no matching tenant is found.
        """
        import re
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")

        tenant = self.nb.tenancy.tenants.get(slug=slug)
        if not tenant:
            raise ValueError(f"No tenant found with slug '{slug}'. Is the lab built in NetBox?")

        site = self._get_site(slug, tenant.id)

        log.info("Reading lab '%s' from NetBox (tenant=%s, site=%s)", name, tenant.id, site.id if site else None)

        # Description and tags from tenant
        description = tenant.description or ""
        tags = [t.slug for t in (tenant.tags or [])]

        # Management prefix from IPAM
        mgmt_prefix = self._get_mgmt_prefix(site.id if site else None, tenant.id)

        lab = LabTopology(
            name=tenant.name,
            description=description,
            mgmt_prefix=mgmt_prefix,
            site_name=site.name if site else None,
            tags=tags,
        )

        # Build devices
        devices_nb = list(self.nb.dcim.devices.filter(tenant_id=tenant.id))
        if not devices_nb:
            log.warning("No devices found for tenant '%s'.", name)
            return lab

        # iface_id → (device_name, iface_name) — used when reconstructing cables
        iface_map: Dict[int, Tuple[str, str]] = {}

        for dev_nb in devices_nb:
            device, dev_iface_map = self._read_device(dev_nb, tenant.id)
            lab.add_device(device)
            iface_map.update(dev_iface_map)

        # Build links from cables
        for link in self._read_links(devices_nb, iface_map):
            lab.add_link(link)

        log.info(
            "Read lab '%s': %d devices, %d links",
            name, len(lab.devices), len(lab.links),
        )
        return lab

    def list_labs(self, tenant_group: str = "Labs") -> List[str]:
        """
        Return the names of all labs in *tenant_group*.

        Args:
            tenant_group: Tenant group name (default ``"Labs"``).
        """
        import re
        slug = re.sub(r"[^a-z0-9-]+", "-", tenant_group.lower()).strip("-")
        group = self.nb.tenancy.tenant_groups.get(slug=slug)
        if not group:
            return []
        return [t.name for t in self.nb.tenancy.tenants.filter(group_id=group.id)]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_site(self, slug: str, tenant_id: int) -> Optional[object]:
        """Find the site for this lab — first by slug, then by tenant."""
        site = self.nb.dcim.sites.get(slug=slug)
        if site:
            return site
        sites = list(self.nb.dcim.sites.filter(tenant_id=tenant_id))
        return sites[0] if sites else None

    def _get_mgmt_prefix(self, site_id: Optional[int], tenant_id: int) -> Optional[str]:
        """Return the management prefix string if one exists for this lab."""
        filters = {"tenant_id": tenant_id, "is_pool": True}
        if site_id:
            filters["site_id"] = site_id
        prefixes = list(self.nb.ipam.prefixes.filter(**filters))
        return str(prefixes[0].prefix) if prefixes else None

    def _read_device(
        self,
        dev_nb,
        tenant_id: int,
    ) -> Tuple[Device, Dict[int, Tuple[str, str]]]:
        """
        Reconstruct a :class:`~lab_tools.topology.Device` from a NetBox device record.

        Returns the Device and a mapping of ``{interface_id: (device_name, iface_name)}``
        for cable reconstruction.
        """
        # Platform and role — fall back gracefully if not in our enums
        platform_slug = dev_nb.platform.slug if dev_nb.platform else "linux"
        role_slug     = dev_nb.role.slug     if dev_nb.role     else "host"

        platform = DevicePlatform.from_slug(platform_slug)
        role     = DeviceRole.from_slug(role_slug)

        # image stored in comments by the builder
        image = dev_nb.comments.strip() if dev_nb.comments else None

        tags = [t.slug for t in (dev_nb.tags or [])]
        custom_fields = dict(dev_nb.custom_fields) if dev_nb.custom_fields else {}

        device = Device(
            name=dev_nb.name,
            platform=platform,
            role=role,
            image=image or None,
            tags=tags,
            custom_fields=custom_fields,
        )
        device._netbox_id = dev_nb.id

        # Interfaces
        iface_map: Dict[int, Tuple[str, str]] = {}
        ifaces_nb = list(self.nb.dcim.interfaces.filter(device_id=dev_nb.id))

        # Primary IP → mgmt_ip (resolve the address string)
        primary_ip_address: Optional[str] = None
        if dev_nb.primary_ip4:
            primary_ip_address = str(dev_nb.primary_ip4.address)

        for iface_nb in ifaces_nb:
            iface_map[iface_nb.id] = (dev_nb.name, iface_nb.name)

            # Resolve IP address assigned to this interface
            ip_address = self._get_interface_ip(iface_nb.id)

            iface_type = InterfaceType.from_netbox(
                iface_nb.type.value if iface_nb.type else "virtual"
            )
            iface = Interface(
                name=iface_nb.name,
                ip_address=ip_address,
                description=iface_nb.description or None,
                iface_type=iface_type,
                enabled=iface_nb.enabled,
                tags=[t.slug for t in (iface_nb.tags or [])],
            )
            iface._netbox_id = iface_nb.id

            # Management interface → set on device, keep in interfaces list too
            if iface_nb.name in _MGMT_IFACE_NAMES:
                device.mgmt_ip = ip_address or primary_ip_address
            else:
                device.interfaces.append(iface)

        # Fallback: if mgmt_ip still unset but primary_ip exists, set it
        if not device.mgmt_ip and primary_ip_address:
            device.mgmt_ip = primary_ip_address

        return device, iface_map

    def _get_interface_ip(self, interface_id: int) -> Optional[str]:
        """Return the first IP address assigned to an interface, as a CIDR string."""
        ips = list(self.nb.ipam.ip_addresses.filter(interface_id=interface_id))
        if ips:
            return str(ips[0].address)
        return None

    def _read_links(
        self,
        devices_nb: list,
        iface_map: Dict[int, Tuple[str, str]],
    ) -> List[Link]:
        """
        Reconstruct :class:`~lab_tools.topology.Link` objects from NetBox cables.

        Iterates cables per device and deduplicates by cable ID.
        Only cables where both endpoints are in this lab's iface_map are included.
        """
        links: List[Link] = []
        seen_cable_ids: set = set()

        for dev_nb in devices_nb:
            cables = self.nb.dcim.cables.filter(device_id=dev_nb.id)
            for cable in cables:
                if cable.id in seen_cable_ids:
                    continue
                seen_cable_ids.add(cable.id)

                link = self._cable_to_link(cable, iface_map)
                if link:
                    links.append(link)

        return links

    def _cable_to_link(
        self,
        cable,
        iface_map: Dict[int, Tuple[str, str]],
    ) -> Optional[Link]:
        """
        Convert a single NetBox cable object to a :class:`~lab_tools.topology.Link`.

        Returns ``None`` if either endpoint isn't in this lab (cross-lab cable)
        or if the terminations aren't interface type.
        """
        a_terms = cable.a_terminations or []
        b_terms = cable.b_terminations or []

        if not a_terms or not b_terms:
            log.debug("Cable id=%s has missing terminations — skipping.", cable.id)
            return None

        # pynetbox returns terminations as objects with object_type / object_id
        a = a_terms[0]
        b = b_terms[0]

        # Only handle interface↔interface cables
        a_type = getattr(a, "object_type", None)
        b_type = getattr(b, "object_type", None)
        if a_type != "dcim.interface" or b_type != "dcim.interface":
            log.debug("Cable id=%s is not interface↔interface — skipping.", cable.id)
            return None

        a_id = getattr(a, "object_id", None)
        b_id = getattr(b, "object_id", None)

        if a_id not in iface_map or b_id not in iface_map:
            log.debug(
                "Cable id=%s endpoints not in this lab's iface_map — skipping (cross-lab?).",
                cable.id,
            )
            return None

        dev_a, iface_a = iface_map[a_id]
        dev_b, iface_b = iface_map[b_id]

        cable_type  = cable.type.value if cable.type else "cat6"
        description = cable.label or None

        link = Link(
            device_a=dev_a,
            interface_a=iface_a,
            device_b=dev_b,
            interface_b=iface_b,
            description=description,
            cable_type=cable_type,
        )
        link._netbox_id = cable.id
        return link
