"""
lab_config/netbox/builder.py
----------------------------
Pushes a LabTopology into NetBox using pynetbox.

Site slug namespacing
---------------------
NetBox sites share a global slug space regardless of tenant.
To keep lab versions isolated, site slugs are prefixed with the lab name:

    LabSite("dc-a")  in  LabTopology("cisco-ospf-v1")
    → slug: cisco-ospf-v1-dc-a
    → name: dc-a   (display name stays short)

Creation order
--------------
1.  Tenant Group  "Labs"         shared, once
2.  Site Group    "Lab Sites"    shared, once
3.  Manufacturer                 shared, once per manufacturer
4.  Device Type                  shared, once per platform model
5.  Device Role                  shared, once per role
6.  Platform                     shared, once per platform
7.  Tenant                       per lab
8.  Sites                        per lab, one per LabSite
9.  IPAM Prefix                  per lab (single mgmt prefix, tenant-scoped)
10. Devices                      per device, assigned to their site
11. Interfaces + IP Addresses
12. Primary IP on device
13. Cables

All steps are idempotent — safe to re-run.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

import pynetbox

from ..topology import (
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


# ---------------------------------------------------------------------------
# Platform reference data
# ---------------------------------------------------------------------------

PLATFORM_META: Dict[DevicePlatform, Dict] = {
    # Arista
    DevicePlatform.CEOS:           {"manufacturer": "Arista Networks",  "model": "cEOS",           "napalm": "eos"},
    DevicePlatform.VEOS:           {"manufacturer": "Arista Networks",  "model": "vEOS",           "napalm": "eos"},
    # Cisco IOS / IOS-XE
    DevicePlatform.IOSV:           {"manufacturer": "Cisco",            "model": "IOSv",            "napalm": "ios"},
    DevicePlatform.IOL:            {"manufacturer": "Cisco",            "model": "IOL",             "napalm": "ios"},
    DevicePlatform.IOLL2:          {"manufacturer": "Cisco",            "model": "IOL L2",          "napalm": None},
    DevicePlatform.CSR1000V:       {"manufacturer": "Cisco",            "model": "CSR1000v",        "napalm": "ios"},
    DevicePlatform.CAT8000V:       {"manufacturer": "Cisco",            "model": "Catalyst8000v",   "napalm": "ios"},
    # Cisco IOS-XR
    DevicePlatform.XRVM:           {"manufacturer": "Cisco",            "model": "XRv",             "napalm": "iosxr"},
    DevicePlatform.XRD:            {"manufacturer": "Cisco",            "model": "XRd",             "napalm": "iosxr"},
    # Cisco NX-OS
    DevicePlatform.NXOSV:          {"manufacturer": "Cisco",            "model": "NX-OSv",          "napalm": "nxos"},
    # Juniper
    DevicePlatform.VJUNOSEVOLVED:  {"manufacturer": "Juniper Networks", "model": "vJunos-evolved",  "napalm": "junos"},
    DevicePlatform.VQFX:           {"manufacturer": "Juniper Networks", "model": "vQFX",            "napalm": "junos"},
    DevicePlatform.VMXVCP:         {"manufacturer": "Juniper Networks", "model": "vMX-VCP",         "napalm": "junos"},
    # Open-source / Linux
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


def _site_slug(lab_name: str, site_name: str) -> str:
    """Namespaced site slug: ``{lab-name}-{site-name}``."""
    return f"{_slug(lab_name)}-{_slug(site_name)}"


def _get_or_create(endpoint, msg: str, **kwargs):
    """Fetch by slug, create if missing. Returns (obj, created)."""
    slug = kwargs.get("slug")
    obj = endpoint.get(slug=slug) if slug else endpoint.get(**kwargs)
    if obj:
        return obj, False
    obj = endpoint.create(**kwargs)
    log.info(msg)
    return obj, True


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class NetBoxLabBuilder:
    """
    Push a LabTopology into NetBox.

    Args:
        nb:                Connected ``pynetbox.api`` instance.
        tenant_group_name: Defaults to ``"Labs"``.
        site_group_name:   Defaults to ``"Lab Sites"``.
    """

    def __init__(
        self,
        nb: pynetbox.api,
        tenant_group_name: str = "Labs",
        site_group_name: str = "Lab Sites",
    ) -> None:
        self.nb = nb
        self.tenant_group_name = tenant_group_name
        self.site_group_name   = site_group_name

        self._tenant_group_id: Optional[int] = None
        self._site_group_id: Optional[int] = None
        self._manufacturer_ids: Dict[str, int] = {}
        self._device_type_ids: Dict[str, int] = {}
        self._device_role_ids: Dict[str, int] = {}
        self._platform_ids: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def build(self, lab: LabTopology) -> None:
        """
        Push *lab* into NetBox end-to-end. Validates first.
        All operations are idempotent — safe to re-run.
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

        # Build all sites first, collect site_name → site_id map
        site_ids: Dict[str, int] = {}
        for lab_site in lab.sites:
            site_id = self._ensure_site(lab, lab_site, tenant_id)
            lab_site._netbox_id = site_id
            site_ids[lab_site.name] = site_id

        if lab.mgmt_prefix:
            self._ensure_prefix(lab.mgmt_prefix, tenant_id)

        for device in lab.devices:
            site_id = site_ids[device.site]
            device._netbox_id = self._ensure_device(device, site_id, tenant_id)
            self._ensure_interfaces(device, tenant_id)

        for link in lab.links:
            self._ensure_cable(lab, link)

        log.info("=== Lab '%s' build complete ===", lab.name)

    def teardown(self, lab: LabTopology, *, dry_run: bool = False) -> List[str]:
        """
        Delete all NetBox objects belonging to *lab*.

        Deletes cables → devices → sites → tenant.
        Shared reference data is left intact.

        Args:
            dry_run: Preview what would be deleted without doing it.

        Returns:
            List of action strings.
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

        sites_nb = list(self.nb.dcim.sites.filter(tenant_id=tenant.id))
        for site in sites_nb:
            msg = f"Delete site '{site.name}' (slug={site.slug})"
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
        )
        return obj.id

    def _ensure_site(self, lab: LabTopology, lab_site: LabSite, tenant_id: int) -> int:
        slug = _site_slug(lab.name, lab_site.name)
        obj, _ = _get_or_create(
            self.nb.dcim.sites,
            f"Created site '{lab_site.name}' (slug={slug})",
            slug=slug,
            name=lab_site.name,
            status="active",
            group=self._site_group_id,
            tenant=tenant_id,
            description=lab_site.description,
        )
        return obj.id

    # ------------------------------------------------------------------
    # IPAM
    # ------------------------------------------------------------------

    def _ensure_prefix(self, prefix: str, tenant_id: int) -> int:
        obj = self.nb.ipam.prefixes.get(prefix=prefix)
        if not obj:
            obj = self.nb.ipam.prefixes.create(
                prefix=prefix,
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
            )
            log.info("Created device '%s'", device.name)
        return obj.id

    def _ensure_interfaces(self, device: Device, tenant_id: int) -> None:
        assert device._netbox_id, f"Device '{device.name}' has no NetBox ID."

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
                )
                log.info("Created interface %s / %s", device.name, iface.name)

            iface._netbox_id = obj.id

            if iface.ip_address:
                ip_id = self._ensure_ip(iface.ip_address, obj.id, tenant_id)
                iface._netbox_ip_id = ip_id
                if iface.name == "Management0":
                    primary_ip_id = ip_id

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
