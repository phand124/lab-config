#!/usr/bin/env python3
"""
nb_lab.py — push, read, or tear down a lab defined in a YAML file.

Usage
-----
    python nb_lab.py cisco-ospf-v1.yaml
    python nb_lab.py cisco-ospf-v1.yaml --read
    python nb_lab.py cisco-ospf-v1.yaml --teardown
    python nb_lab.py cisco-ospf-v1.yaml --teardown --dry-run
"""

import argparse
import logging
import sys

from lab_tools.loader import load_yaml
from lab_tools.netbox import connect, NetBoxLabBuilder, NetBoxLabReader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage a lab topology in NetBox.")
    parser.add_argument("topology", help="Path to the lab YAML file.")
    parser.add_argument("--read",     action="store_true", help="Pull lab back from NetBox and print.")
    parser.add_argument("--teardown", action="store_true", help="Remove lab from NetBox.")
    parser.add_argument("--dry-run",  action="store_true", help="With --teardown: preview only.")
    args = parser.parse_args()

    nb = connect()

    if args.read:
        lab = load_yaml(args.topology)
        lab = NetBoxLabReader(nb).get_lab(lab.name)
        print(f"\nLab   : {lab.name}")
        print(f"Desc  : {lab.description}")
        print(f"Mgmt  : {lab.mgmt_prefix}")
        print(f"\nSites ({len(lab.sites)}):")
        for s in lab.sites:
            print(f"  {s.name:<20}  {s.description}")
        print(f"\nDevices ({len(lab.devices)}):")
        for d in lab.devices:
            print(f"  [{d.site}] {d.name:<10}  platform={d.platform.value:<10}  mgmt={d.mgmt_ip}")
            for i in d.interfaces:
                print(f"    {i.name:<30}  {i.ip_address or ''}")
        print(f"\nLinks ({len(lab.links)}):")
        for link in lab.links:
            print(f"  {link.device_a}:{link.interface_a}  ↔  {link.device_b}:{link.interface_b}")
        return

    if args.teardown:
        lab     = load_yaml(args.topology)
        actions = NetBoxLabBuilder(nb).teardown(lab, dry_run=args.dry_run)
        if args.dry_run:
            print("\nDry-run — would delete:")
            for a in actions:
                print(f"  {a}")
        return

    lab = load_yaml(args.topology)
    NetBoxLabBuilder(nb).build(lab)
    print(f"\n✓  Lab '{lab.name}' is in NetBox.")
    print(f"   Tenant  : {lab.name}")
    print(f"   Sites   : {', '.join(s.name for s in lab.sites)}")
    print(f"   Devices : {', '.join(f'{d.name} [{d.site}]' for d in lab.devices)}")
    print(f"   Links   : {len(lab.links)}")


if __name__ == "__main__":
    main()
