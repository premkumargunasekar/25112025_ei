#!/usr/bin/env python3
from __future__ import annotations
import sys
import csv
import os
import re
import json
import subprocess
import ipaddress
from typing import Optional

# ---------------------------
# ARG PARSING
# ---------------------------
if len(sys.argv) < 6:
    sys.stderr.write("Usage: calc_next_subnet.py <block> <subnet_size> <subnet_csv> <gcp_project> <gcp_region> [n_region n_env n_solution n_gcpregion]\n")
    sys.exit(2)

block_arg = sys.argv[1]
subnet_size = int(sys.argv[2])
subnet_csv = sys.argv[3]
gcp_project = sys.argv[4]
gcp_region = sys.argv[5]

# optional naming inputs (CLI override env)
n_region = sys.argv[6] if len(sys.argv) >= 7 else None
n_env = sys.argv[7] if len(sys.argv) >= 8 else None
n_solution = sys.argv[8] if len(sys.argv) >= 9 else None
n_gcpregion_override = sys.argv[9] if len(sys.argv) >= 10 else None

# fallback to env vars
n_region = n_region or os.environ.get("NAMING_REGION")
n_env = n_env or os.environ.get("NAMING_ENV")
n_solution = n_solution or os.environ.get("NAMING_SOLUTION")
n_gcpregion_override = n_gcpregion_override or os.environ.get("NAMING_GCPREGION")

# num width
try:
    NUM_WIDTH = int(os.environ.get("NAMING_NUM_WIDTH", "3"))
except Exception:
    NUM_WIDTH = 3

# ---------------------------
# Data structures
# ---------------------------
# used_networks stores ipaddress.IPv4Network/IPv6Network objects
used_networks: set[ipaddress._BaseNetwork] = set()

# name_to_cidr maps subnet name -> ip_network object (when available)
name_to_cidr: dict[str, ipaddress._BaseNetwork] = {}

# Helper: add a cidr to used_networks (silently ignore bad values)
def add_cidr_to_used(cidr_str: str) -> Optional[ipaddress._BaseNetwork]:
    if not cidr_str:
        return None
    try:
        net = ipaddress.ip_network(cidr_str.strip())
        used_networks.add(net)
        return net
    except Exception:
        return None

# ---------------------------
# Read CSV: collect CIDRs and name->cidr mapping
# CSV may contain columns: SubnetCIDR, CIDR, SubnetName, SubnetName (with varied capitalization)
# ---------------------------
def read_subnet_csv(path: str):
    if not os.path.exists(path):
        return
    try:
        with open(path, newline='') as f:
            # tolerate embedded nulls
            reader = csv.DictReader((line.replace('\0', '') for line in f))
            for row in reader:
                # find any plausible CIDR field
                cidr_val = None
                if "SubnetCIDR" in row and row.get("SubnetCIDR"):
                    cidr_val = row.get("SubnetCIDR")
                elif "CIDR" in row and row.get("CIDR"):
                    cidr_val = row.get("CIDR")
                else:
                    # fallback: first value that looks like a cidr
                    for v in row.values():
                        if isinstance(v, str) and '/' in v:
                            cidr_val = v
                            break
                net_obj = None
                if cidr_val:
                    net_obj = add_cidr_to_used(cidr_val)

                # try to map SubnetName -> CIDR when available
                name_val = None
                for key in ("SubnetName", "SubnetName ".strip(), "Subnet Name", "subnetname", "subnet_name"):
                    if key in row and row.get(key):
                        name_val = row.get(key)
                        break
                # also try generic columns that look like short tokens and contain "snt" suffix
                if not name_val:
                    for v in row.values():
                        if isinstance(v, str):
                            vv = v.strip()
                            # heuristic: contains "-snt" or matches common naming length
                            if vv.endswith("-snt") or (len(vv) <= 40 and '-' in vv):
                                name_val = vv
                                break

                if name_val and net_obj:
                    try:
                        name_to_cidr[name_val.strip()] = net_obj
                    except Exception:
                        pass
    except Exception as e:
        sys.stderr.write(f"Warning: unable to read CSV {path}: {e}\n")

# ---------------------------
# Query GCP with gcloud (if available)
# Populate used_networks and name_to_cidr
# ---------------------------
def query_gcp_subnets(project: str, region: str):
    try:
        cmd = [
            "gcloud", "compute", "networks", "subnets", "list",
            "--project", project,
            "--regions", region,
            "--format=json"
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=30)
        arr = json.loads(output)
        for s in arr:
            ip = s.get("ipCidrRange")
            name = s.get("name")
            if ip:
                net_obj = add_cidr_to_used(ip)
                if name and net_obj:
                    # map name to network object
                    name_to_cidr[name] = net_obj
    except FileNotFoundError:
        sys.stderr.write("gcloud binary not found; falling back to CSV-only view. THIS IS LESS RELIABLE.\n")
    except subprocess.CalledProcessError as e:
        stderr_msg = ""
        try:
            stderr_msg = e.output.decode('utf-8', errors='ignore')
        except Exception:
            stderr_msg = str(e)
        sys.stderr.write(f"gcloud returned error: {stderr_msg}\n")
    except Exception as e:
        sys.stderr.write(f"Warning: could not fetch GCP subnets ({e})\n")

# ---------------------------
# Infer naming parts from CSV (if not provided)
# ---------------------------
def infer_from_csv_for(path: str, colnames):
    if not os.path.exists(path):
        return None
    try:
        with open(path, newline='') as f:
            reader = csv.DictReader((line.replace('\0', '') for line in f))
            for row in reader:
                for c in colnames:
                    if c in row and row.get(c):
                        v = row.get(c).strip()
                        if v:
                            return v
                # fallback: first short non-cidr token
                for v in row.values():
                    if isinstance(v, str):
                        vv = v.strip()
                        if vv and '/' not in vv and len(vv) <= 20:
                            return vv
    except Exception:
        return None
    return None

# ---------------------------
# Normalize naming pieces
# ---------------------------
def norm_token(t: Optional[str], default: str):
    if not t:
        return default
    t2 = re.sub(r'[^a-z0-9\-]', '-', t.lower())
    # collapse repetitive hyphens
    t2 = re.sub(r'-{2,}', '-', t2).strip('-')
    return t2 or default

# ---------------------------
# Sequence helpers
# ---------------------------
def pad_seq(n: int) -> str:
    return str(n).zfill(NUM_WIDTH)

# ---------------------------
# Main allocation logic
# ---------------------------
def find_next_free(block: ipaddress._BaseNetwork, prefix_seq_start: int, subnet_prefixlen: int, max_seq_search: int = 1000):
    """
    Iterate subnets in a block and find first subnet (by ascending order) that:
     - does not overlap any used_networks (we treat ANY overlap as collision)
     - has a sequence number (try start..start+max_seq_search) whose expected_name
       is either not present in name_to_cidr OR maps to the exact same cidr
    Returns (network_obj, seq_int) or (None, None)
    """
    for candidate in block.subnets(new_prefix=subnet_prefixlen):
        # if candidate overlaps any used network, skip
        collision = False
        for used in used_networks:
            # if either overlaps the other, treat as collision
            if used.overlaps(candidate) or candidate.overlaps(used):
                collision = True
                break
        if collision:
            continue

        # try sequence numbers
        seq = prefix_seq_start
        tried = 0
        while tried < max_seq_search:
            expected_name = expected_name_for_seq(seq)
            mapped = name_to_cidr.get(expected_name)
            if mapped is None:
                # name not present -> safe
                return candidate, seq
            else:
                # if mapped network equals candidate (idempotent), safe
                # compare network objects
                try:
                    if mapped == candidate:
                        return candidate, seq
                except Exception:
                    pass
            seq += 1
            tried += 1
        # if sequences exhausted for this candidate, move to next candidate
    return None, None

# ---------------------------
# Build expected name for sequence
# ---------------------------
def expected_name_for_seq_seqbuilder(prefix: str, seq: int) -> str:
    return f"{prefix}{pad_seq(seq)}-snt"

# We'll assign expected_name_for_seq later after prefix is known.

# ---------------------------
# Run
# ---------------------------
def main():
    # parse block
    try:
        block = ipaddress.ip_network(block_arg)
    except Exception as e:
        sys.stderr.write(f"Invalid block '{block_arg}': {e}\n")
        sys.exit(2)

    # read CSV
    read_subnet_csv(subnet_csv)

    # query gcp (best-effort)
    query_gcp_subnets(gcp_project, gcp_region)

    # infer naming pieces if not provided
    nonlocal_n_region = n_region
    nonlocal_n_env = n_env
    nonlocal_n_solution = n_solution
    nonlocal_n_gcpregion_override = n_gcpregion_override

    if not nonlocal_n_region:
        nonlocal_n_region = infer_from_csv_for(subnet_csv, ["RegionCode", "Region", "region", "Region_Code", "region_code"]) or "as"
    if not nonlocal_n_env:
        nonlocal_n_env = infer_from_csv_for(subnet_csv, ["Env", "Environment", "env"]) or "pr"
    if not nonlocal_n_solution:
        nonlocal_n_solution = infer_from_csv_for(subnet_csv, ["Solution", "SolutionName", "solution"]) or "linux"
    if not nonlocal_n_gcpregion_override:
        nonlocal_n_gcpregion_override = gcp_region

    # normalize
    part_region = norm_token(nonlocal_n_region, "as")
    part_env = norm_token(nonlocal_n_env, "pr")
    part_solution = norm_token(nonlocal_n_solution, "linux")
    part_gcpregion = norm_token(nonlocal_n_gcpregion_override, gcp_region)

    prefix = f"{part_region}-{part_env}-nw-{part_solution}-{part_gcpregion}-"
    # seq regex
    seq_re = re.compile(re.escape(prefix) + r'(\d+)-snt$')

    # compute existing seq numbers
    existing_seq_nums = []
    for nm in name_to_cidr.keys():
        m = seq_re.match(nm)
        if m:
            try:
                existing_seq_nums.append(int(m.group(1)))
            except Exception:
                pass
    max_seq = max(existing_seq_nums) if existing_seq_nums else 0
    base_next_seq = max_seq + 1

    # set expected_name_for_seq for closure usage
    global expected_name_for_seq
    expected_name_for_seq = lambda s: expected_name_for_seq_seqbuilder(prefix, s)

    # find next free
    candidate_net, candidate_seq = find_next_free(block, base_next_seq, subnet_size, max_seq_search=5000)
    if candidate_net is None:
        print("NO_AVAILABLE_SUBNET")
        sys.exit(1)

    # print in format expected by playbook
    try:
        out_cidr = str(candidate_net)
        out_seq = pad_seq(candidate_seq)
        print(f"{out_cidr}|{out_seq}")
        sys.exit(0)
    except Exception as e:
        sys.stderr.write(f"Error preparing output: {e}\n")
        print("NO_AVAILABLE_SUBNET")
        sys.exit(1)

if __name__ == "__main__":
    # define placeholder for expected_name_for_seq to be replaced in main
    expected_name_for_seq = lambda s: str(s)
    main()

