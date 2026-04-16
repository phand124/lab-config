"""
lab_tools/netbox/builder.py
---------------------------
Pushes a LabTopology into NetBox using pynetbox.

Creation order
--------------
1.  Tenant Group  "Labs"        shared, once
2.  Site Group    "Lab Sites"   shared, once
3.  Manufacturer               shared, once per manufacturer
4.  Device Type                shared, once per platform model
5.  Device Role                shared, once per role
6.  Platform                   shared, once per platform
7.  Tenant                     per lab
8.  Site                       per lab
9.  IPAM Prefix                per lab  (if mgmt_prefix set)
10. Devices
11. Interfaces + IP Addresses
12. Primary IP set on device   (from mgmt_ip)
13. Cables

All steps are idempotent — safe to re-run.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

import pynetbox

from ..topology import Device, DevicePlatform, DeviceRole, Interface, InterfaceType, LabTopology, Link

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reference data: platform → (manufacturer, device-type model, napalm driver)
# ---------------------------------------------------------------------------

PLATFORM_META: Dict[DevicePlatform, Dict] = {
    DevicePlatform.CEOS:           {"manufacturer": "Arista Networks",  "model": "cEOS",           "napalm": "eos"},
    DevicePlatform.VEOS:           {"manufacturer": "Arista Networks",  "model": "vEOS",           "napalm": "eos"},
    DevicePlatform.IOL:            {"manufacturer": "Cisco", "model": "IOL",    "napalm": "ios"},
    DevicePlatform.IOLL2:          {"manufacturer": "Cisco", "model": "IOL L2", "napalm": None},
    DevicePlatform.IOSV:           {"manufacturer": "Cisco",            "model": "IOSv",            "napalm": "ios"},
    DevicePlatform.CSR1000V:       {"manufacturer": "Cisco",            "model": "CSR1000v",        "napalm": "ios"},
    DevicePlatform.CAT8000V:       {"manufacturer": "Cisco",            "model": "Catalyst8000v",   "napalm": "ios"},
    DevicePlatform.XRVM:           {"manufacturer": "Cisco",            "model": "XRv",             "napalm": "iosxr"},
    DevicePlatform.XRD:            {"manufacturer": "Cisco",            "model": "XRd",             "napalm": "iosxr"},
    DevicePlatform.NXOSV:          {"manufacturer": "Cisco",            "model": "NX-OSv",          "napalm": "nxos"},
    DevicePlatform.VJUNOSEVOLVED:  {"manufacturer": "Juniper Networks", "model": "vJunos-evolved",  "napalm": "junos"},
    DevicePlatform.VQFX:           {"manufacturer": "Juniper Networks", "model": "vQFX",            "napalm": "junos"},
    DevicePlatform.VMXVCP:         {"manufacturer": "Juniper Networks", "model": "vMX-VCP",         "napalm": "junos"},
    DevicePlatform.LINUX:          {"manufacturer": "Generic",          "model": "Linux VM",        "napalm": None},
    DevicePlatform.VYOS:           {"manufacturer": "VyOS",             "model": "VyOS",            "napalm": None},
    DevicePlatform.FRR:            {"manufacturer": "FRRouting",        "model": "FRR",             "napalm": None},
    DevicePlatform.OPENWRT:        {"manufacturer": "OpenWrt",          "model": "OpenWrt",         "napalm": None},
}

ROLE_COLORS: Dict[DeviceRole, str] = {
    DeviceRole.ROUTER:   "2196f3",
    DeviceRole.SWITCH:   "4caf50",
    DeviceRole.FIREWALL: "f44336",
    DeviceRole.SERVER:   "9c27b0",
    DeviceRole.HOST:     "ff9800",
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")


def _get_or_create(endpoint, created_msg: str, **kwargs):
    """
    Fetch a single object by kwargs. Create it if it doesn't exist.
    Returns (object, created: bool).
    """
    obj = endpoint.get(**kwargs)
    if obj:
        return obj, False
    obj = endpoint.create(**kwargs)
    log.info(created_msg)
    return obj, True


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class NetBoxLabBuilder:
    """
    Push a :class:`~lab_tools.topology.LabTopology` into NetBox.

    Args:
        nb:                Connected ``pynetbox.api`` instance.
        tenant_group_name: Defaults to ``"Labs"``.
        site_group_name:   Defaults to ``"Lab Sites"``.

    Example::

        from lab_tools.netbox import connect, NetBoxLabBuilder

        nb      = connect("http://netbox.lab:8000", token="abc123", verify_ssl=False)
        builder = NetBoxLabBuilder(nb)
        builder.build(my_lab)
    """

    def __init__(
        self,
        nb: pynetbox.api,
        tenant_group_name: str = "Labs",
        site_group_name: str = "Lab Sites",
    ) -> None:
        self.nb = nb
        self.tenant_group_name = tenant_group_name
        self.site_group_name = site_group_name

        # Caches — populated progressively, shared across multiple build() calls
        self._tenant_group_id: Optional[int] = None
        self._site_group_id: Optional[int] = None
        self._manufacturer_ids: Dict[str, int] = {}   # keyed by slug
        self._device_type_ids: Dict[str, int] = {}    # keyed by slug
        self._device_role_ids: Dict[str, int] = {}    # keyed by slug
        self._platform_ids: Dict[str, int] = {}       # keyed by platform.value

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def build(self, lab: LabTopology) -> None:
        """
        Push *lab* into NetBox end-to-end. Validates topology first.
        Safe to call multiple times — all operations are idempotent.
        """
        errors = lab.validate()
        if errors:
            raise ValueError(
                f"Topology '{lab.name}' has validation errors:\n"
                + "\n".join(f"  • {e}" for e in errors)
            )

        log.info("=== Building lab '%s' ===", lab.name)
        self._ensure_shared_reference_data(lab)
        tenant_id = self._ensure_tenant(lab)
        site_id   = self._ensure_site(lab, tenant_id)

        if lab.mgmt_prefix:
            self._ensure_prefix(lab.mgmt_prefix, site_id, tenant_id)

        for device in lab.devices:
            device._netbox_id = self._ensure_device(device, site_id, tenant_id)
            self._ensure_interfaces(device, tenant_id)

        for link in lab.links:
            self._ensure_cable(lab, link)

        log.info("=== Lab '%s' build complete ===", lab.name)

    def teardown(self, lab: LabTopology, *, dry_run: bool = False) -> List[str]:
        """
        Delete all NetBox objects belonging to *lab*.

        Shared reference data (manufacturers, device types, roles, platforms)
        is intentionally preserved — other labs may use them.

        Args:
            dry_run: Return what would be deleted without deleting it.

        Returns:
            List of action strings describing what was (or would be) deleted.
        """
        actions: List[str] = []
        tenant = self.nb.tenancy.tenants.get(slug=_slug(lab.name))
        if not tenant:
            log.warning("No tenant for '%s' — nothing to remove.", lab.name)
            return actions

        # Cables first to avoid FK constraint errors
        devices_nb = list(self.nb.dcim.devices.filter(tenant_id=tenant.id))
        seen_cable_ids: set = set()
        for dev in devices_nb:
            for cable in self.nb.dcim.cables.filter(device_id=dev.id):
                if cable.id in seen_cable_ids:
                    continue
                seen_cable_ids.add(cable.id)
                msg = f"Delete cable id={cable.id}"
                actions.append(msg)
                if not dry_run:
                    log.info(msg)
                    cable.delete()

        for dev in devices_nb:
            msg = f"Delete device '{dev.name}'"
            actions.append(msg)
            if not dry_run:
                log.info(msg)
                dev.delete()

        site = self.nb.dcim.sites.get(slug=_slug(lab.site_name or lab.name))
        if site:
            msg = f"Delete site '{site.name}'"
            actions.append(msg)
            if not dry_run:
                log.info(msg)
                site.delete()

        msg = f"Delete tenant '{tenant.name}'"
        actions.append(msg)
        if not dry_run:
            log.info(msg)
            tenant.delete()

        return actions

    # ------------------------------------------------------------------
    # Shared reference data
    # ------------------------------------------------------------------

    def _ensure_shared_reference_data(self, lab: LabTopology) -> None:
        self._ensure_tenant_group()
        self._ensure_site_group()
        for platform in {d.platform for d in lab.devices}:
            self._ensure_manufacturer(platform)
            self._ensure_device_type(platform)
            self._ensure_platform(platform)
        for role in {d.role for d in lab.devices}:
            self._ensure_device_role(role)

    def _ensure_tenant_group(self) -> int:
        if self._tenant_group_id:
            return self._tenant_group_id
        slug = _slug(self.tenant_group_name)
        obj, _ = _get_or_create(
            self.nb.tenancy.tenant_groups,
            f"Created tenant group '{self.tenant_group_name}'",
            slug=slug, name=self.tenant_group_name,
        )
        self._tenant_group_id = obj.id
        return obj.id

    def _ensure_site_group(self) -> int:
        if self._site_group_id:
            return self._site_group_id
        slug = _slug(self.site_group_name)
        obj, _ = _get_or_create(
            self.nb.dcim.site_groups,
            f"Created site group '{self.site_group_name}'",
            slug=slug, name=self.site_group_name,
        )
        self._site_group_id = obj.id
        return obj.id

    def _ensure_manufacturer(self, platform: DevicePlatform) -> int:
        name = PLATFORM_META[platform]["manufacturer"]
        slug = _slug(name)
        if slug not in self._manufacturer_ids:
            obj, _ = _get_or_create(
                self.nb.dcim.manufacturers,
                f"Created manufacturer '{name}'",
                slug=slug, name=name,
            )
            self._manufacturer_ids[slug] = obj.id
        return self._manufacturer_ids[slug]

    def _ensure_device_type(self, platform: DevicePlatform) -> int:
        meta    = PLATFORM_META[platform]
        model   = meta["model"]
        dt_slug = _slug(model)
        if dt_slug not in self._device_type_ids:
            mfr_id = self._manufacturer_ids[_slug(meta["manufacturer"])]
            obj, _ = _get_or_create(
                self.nb.dcim.device_types,
                f"Created device type '{model}'",
                slug=dt_slug, model=model, manufacturer=mfr_id,
            )
            self._device_type_ids[dt_slug] = obj.id
        return self._device_type_ids[dt_slug]

    def _ensure_device_role(self, role: DeviceRole) -> int:
        slug = role.value
        if slug not in self._device_role_ids:
            obj, _ = _get_or_create(
                self.nb.dcim.device_roles,
                f"Created device role '{role.value}'",
                slug=slug,
                name=role.value.capitalize(),
                color=ROLE_COLORS.get(role, "9e9e9e"),
                vm_role=False,
            )
            self._device_role_ids[slug] = obj.id
        return self._device_role_ids[slug]

    def _ensure_platform(self, platform: DevicePlatform) -> int:
        slug = platform.value
        if slug not in self._platform_ids:
            meta  = PLATFORM_META[platform]
            extra = {"napalm_driver": meta["napalm"]} if meta["napalm"] else {}
            obj, _ = _get_or_create(
                self.nb.dcim.platforms,
                f"Created platform '{slug}'",
                slug=slug,
                name=platform.value,
                manufacturer=self._manufacturer_ids.get(_slug(meta["manufacturer"])),
                **extra,
            )
            self._platform_ids[slug] = obj.id
        return self._platform_ids[slug]

    # ------------------------------------------------------------------
    # Per-lab objects
    # ------------------------------------------------------------------

    def _ensure_tenant(self, lab: LabTopology) -> int:
        slug = _slug(lab.name)
        obj, _ = _get_or_create(
            self.nb.tenancy.tenants,
            f"Created tenant '{lab.name}'",
            slug=slug,
            name=lab.name,
            group=self._tenant_group_id,
            description=lab.description,
            tags=[{"name": t} for t in lab.tags],
        )
        return obj.id

    def _ensure_site(self, lab: LabTopology, tenant_id: int) -> int:
        site_name = lab.site_name or lab.name
        slug      = _slug(site_name)
        obj, _ = _get_or_create(
            self.nb.dcim.sites,
            f"Created site '{site_name}'",
            slug=slug,
            name=site_name,
            status="active",
            group=self._site_group_id,
            tenant=tenant_id,
            description=lab.description,
        )
        return obj.id

    # ------------------------------------------------------------------
    # IPAM
    # ------------------------------------------------------------------

    def _ensure_prefix(self, prefix: str, site_id: int, tenant_id: int) -> int:
        obj = self.nb.ipam.prefixes.get(prefix=prefix)
        if not obj:
            obj = self.nb.ipam.prefixes.create(
                prefix=prefix,
                site=site_id,
                tenant=tenant_id,
                description="Lab management network",
                is_pool=True,
                status="active",
            )
            log.info("Created prefix %s", prefix)
        return obj.id

    def _ensure_ip(self, address: str, interface_id: int, tenant_id: int) -> int:
        obj = self.nb.ipam.ip_addresses.get(address=address)
        if obj:
            # Re-assign to this interface if needed
            if obj.assigned_object_id != interface_id:
                obj.assigned_object_type = "dcim.interface"
                obj.assigned_object_id   = interface_id
                obj.save()
            return obj.id

        obj = self.nb.ipam.ip_addresses.create(
            address=address,
            assigned_object_type="dcim.interface",
            assigned_object_id=interface_id,
            tenant=tenant_id,
            status="active",
        )
        log.info("Created IP %s", address)
        return obj.id

    # ------------------------------------------------------------------
    # Devices and interfaces
    # ------------------------------------------------------------------

    def _ensure_device(self, device: Device, site_id: int, tenant_id: int) -> int:
        obj = self.nb.dcim.devices.get(name=device.name, site_id=site_id)
        if not obj:
            obj = self.nb.dcim.devices.create(
                name=device.name,
                site=site_id,
                tenant=tenant_id,
                device_type=self._device_type_ids[_slug(PLATFORM_META[device.platform]["model"])],
                role=self._device_role_ids[device.role.value],
                platform=self._platform_ids[device.platform.value],
                status="planned",
                comments=device.image or "",
                tags=[{"name": t} for t in device.tags],
                custom_fields=device.custom_fields,
            )
            log.info("Created device '%s'", device.name)
        return obj.id

    def _ensure_interfaces(self, device: Device, tenant_id: int) -> None:
        assert device._netbox_id, f"Device '{device.name}' has no NetBox ID."

        # Build full interface list including auto mgmt interface
        all_ifaces = list(device.interfaces)
        if device.mgmt_ip and not any(i.name == "Management0" for i in all_ifaces):
            all_ifaces.append(Interface(
                name="Management0",
                ip_address=device.mgmt_ip,
                description="OOB Management",
                iface_type=InterfaceType.VIRTUAL,
            ))

        primary_ip_id: Optional[int] = None

        for iface in all_ifaces:
            obj = self.nb.dcim.interfaces.get(device_id=device._netbox_id, name=iface.name)
            if not obj:
                obj = self.nb.dcim.interfaces.create(
                    device=device._netbox_id,
                    name=iface.name,
                    type=iface.iface_type.value,
                    enabled=iface.enabled,
                    description=iface.description or "",
                    tags=[{"name": t} for t in iface.tags],
                )
                log.info("Created interface %s / %s", device.name, iface.name)

            iface._netbox_id = obj.id

            if iface.ip_address:
                ip_id = self._ensure_ip(iface.ip_address, obj.id, tenant_id)
                iface._netbox_ip_id = ip_id
                if iface.name == "Management0":
                    primary_ip_id = ip_id

        # Set primary IP on the device
        if primary_ip_id:
            dev_obj = self.nb.dcim.devices.get(device._netbox_id)
            dev_obj.primary_ip4 = primary_ip_id
            dev_obj.save()
            log.info("Set primary IPv4 on '%s' → %s", device.name, device.mgmt_ip)

    # ------------------------------------------------------------------
    # Cables
    # ------------------------------------------------------------------

    def _ensure_cable(self, lab: LabTopology, link: Link) -> None:
        dev_a   = lab.get_device(link.device_a)
        dev_b   = lab.get_device(link.device_b)
        iface_a = dev_a.get_interface(link.interface_a) if dev_a else None
        iface_b = dev_b.get_interface(link.interface_b) if dev_b else None

        if not (iface_a and iface_a._netbox_id and iface_b and iface_b._netbox_id):
            log.warning(
                "Skipping cable %s:%s ↔ %s:%s — interfaces missing NetBox IDs.",
                link.device_a, link.interface_a, link.device_b, link.interface_b,
            )
            return

        # Check if a cable already exists on the A-side endpoint
        iface_obj = self.nb.dcim.interfaces.get(iface_a._netbox_id)
        if iface_obj and iface_obj.cable:
            link._netbox_id = iface_obj.cable.id
            log.debug("Cable already exists for %s:%s", link.device_a, link.interface_a)
            return

        cable = self.nb.dcim.cables.create(
            a_terminations=[{"object_type": "dcim.interface", "object_id": iface_a._netbox_id}],
            b_terminations=[{"object_type": "dcim.interface", "object_id": iface_b._netbox_id}],
            type=link.cable_type,
            status="planned",
            label=link.description or "",
        )
        link._netbox_id = cable.id
        log.info(
            "Created cable %s:%s ↔ %s:%s",
            link.device_a, link.interface_a, link.device_b, link.interface_b,
        )
