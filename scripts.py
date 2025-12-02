#!/usr/bin/env python3
import ipaddress
import sys
import csv
import subprocess
import json
import os

if len(sys.argv) < 6:
    sys.stderr.write("Usage: calc_next_subnet.py <block> <subnet_size> <subnet_csv> <gcp_project> <gcp_region>\n")
    sys.exit(2)

block = ipaddress.ip_network(sys.argv[1])
subnet_size = int(sys.argv[2])
subnet_csv = sys.argv[3]
gcp_project = sys.argv[4]
gcp_region = sys.argv[5]

used = set()

def add_cidr(cidr_str):
    cidr_str = cidr_str.strip()
    if not cidr_str:
        return
    try:
        net = ipaddress.ip_network(cidr_str)
        used.add(str(net))
    except Exception:
        return

if os.path.exists(subnet_csv):
    with open(subnet_csv, newline='') as f:
        reader = csv.DictReader((line.replace('\0', '') for line in f))
        for row in reader:
            cidr = None
            if "SubnetCIDR" in row and row.get("SubnetCIDR"):
                cidr = row.get("SubnetCIDR")
            elif "CIDR" in row and row.get("CIDR"):
                cidr = row.get("CIDR")
            else:
                for v in row.values():
                    if isinstance(v, str) and '/' in v:
                        cidr = v
                        break
            if cidr:
                add_cidr(cidr)

try:
    cmd = [
        "gcloud", "compute", "networks", "subnets", "list",
        "--project", gcp_project,
        "--regions", gcp_region,
        "--format=json"
    ]
    output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=30)
    gcp_subnets = json.loads(output)
    for s in gcp_subnets:
        ip = s.get("ipCidrRange")
        if ip:
            add_cidr(ip)
except subprocess.CalledProcessError as e:
    sys.stderr.write("gcloud returned error: {}\n".format(e.output.decode('utf-8', errors='ignore')))
except FileNotFoundError:
    sys.stderr.write("gcloud binary not found; skipping GCP lookup.\n")
except Exception as e:
    sys.stderr.write("Warning: could not fetch GCP subnets ({})\n".format(str(e)))

used_networks = set()
for u in used:
    try:
        used_networks.add(ipaddress.ip_network(u))
    except Exception:
        pass

for subnet in block.subnets(new_prefix=subnet_size):
    if subnet not in used_networks:
        print(str(subnet))
        sys.exit(0)

print("NO_AVAILABLE_SUBNET")
sys.exit(1)
