#!/usr/bin/env python3
import argparse
import getpass
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
except ImportError:
    print('\nERROR: Python module "requests" not found, exiting!\n')
    sys.exit()

try:
    import keyring
except ImportError:
    keyring = None

# ── Region → Zabbix server mapping ───────────────────────────────────────────

ZABBIX_SERVERS = {
    'ch3': '10.103.6.15',
    'ca' : '10.230.113.36',
    'br' : '10.230.111.36',
    'lo5': '10.254.0.227',
    'de' : '10.232.100.4',
    'sg' : '10.230.115.36',
    's1' : '10.250.0.227',
}

REGION_RE = re.compile(r'(ch3|ca|br|lo5|de|sg|jpe|s1)')

# ── Zabbix API helpers ────────────────────────────────────────────────────────

def zabbix_request(url, method, params, auth=None, timeout=300):
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    if auth:
        payload["auth"] = auth

    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Zabbix API error [{method}]: {data['error']['data']}")
    return data["result"]


def login(server_ip, user, password):
    url = f"http://{server_ip}/api_jsonrpc.php"
    return url, zabbix_request(url, "user.login", {"user": user, "password": password})


def logout(url, auth):
    try:
        zabbix_request(url, "user.logout", {}, auth)
    except Exception:
        pass


def wildcard_search(url, auth, hostname):
    """Search for a host using wildcard — matches hostname and hostname.local in one call."""
    result = zabbix_request(
        url,
        "host.get",
        {
            "output": ["hostid", "host"],
            "search": {"host": f"{hostname}*"},
            "searchWildcardsEnabled": True,
        },
        auth,
    )
    return result  # list of {hostid, host}


def delete_one_host(url, auth, resolved, hid, timeout):
    """Delete a single host. Returns (resolved, hid, error_or_None)."""
    try:
        zabbix_request(url, "host.delete", [hid], auth, timeout=timeout)
        return (resolved, hid, None)
    except Exception as e:
        return (resolved, hid, str(e))


# ── Helpers ───────────────────────────────────────────────────────────────────

def read_hostnames(path):
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def get_credentials(username_arg):
    """Get Zabbix username + password, using macOS keyring if available."""
    if username_arg:
        z_user = username_arg
    elif sys.platform == "darwin":
        z_user = input("\nEnter your Zabbix username: ").strip()
    else:
        z_user = getpass.getuser()

    z_passwd = None
    if sys.platform == "darwin" and keyring:
        try:
            z_passwd = keyring.get_password("zabbix", z_user)
        except Exception:
            pass

    if not z_passwd:
        z_passwd = getpass.getpass(prompt="\nEnter your Zabbix password: ")

    return z_user, z_passwd


def group_by_region(hostnames):
    """Group hostnames by region code. Returns {region: [hostnames]}, missing: [hostnames]."""
    by_region = defaultdict(list)
    missing_region = []
    for h in hostnames:
        match = REGION_RE.search(h)
        if match:
            by_region[match.group(1)].append(h)
        else:
            print(f"⚠  No region found in hostname '{h}', skipping!")
            missing_region.append(h)
    return by_region, missing_region


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Delete Zabbix hosts listed in a text file.")
    parser.add_argument("--user",     "-n", help="Zabbix username")
    parser.add_argument("--password",       help="Zabbix password (omit to be prompted)")
    parser.add_argument("--file",     "-f", required=True, help="Text file with one hostname per line")
    parser.add_argument("--dry-run",        action="store_true", help="Show what would be deleted without deleting")
    parser.add_argument("--timeout",        type=int, default=300, help="Seconds per delete call (default: 300)")
    parser.add_argument("--workers",        type=int, default=5,   help="Parallel deletes per region (default: 5)")
    args = parser.parse_args()

    # 1. Read hostnames
    hostnames = read_hostnames(args.file)
    if not hostnames:
        print("No hostnames found in file. Exiting.")
        sys.exit(0)
    print(f"\nLoaded {len(hostnames)} hostname(s) from '{args.file}'.")

    # 2. Get credentials
    z_user, z_passwd = get_credentials(args.user) if not args.password else (args.user, args.password)

    # 3. Group by region
    by_region, no_region = group_by_region(hostnames)
    if not by_region:
        print("No hostnames matched a known region. Exiting.")
        sys.exit(1)

    # 4. Per-region: login, wildcard search, collect hosts to delete
    all_found   = []   # list of (url, auth, resolved_name, hostid)
    all_missing = list(no_region)

    for region, region_hosts in by_region.items():
        server_ip = ZABBIX_SERVERS.get(region)
        if not server_ip:
            print(f"\n⚠  No Zabbix server configured for region '{region}', skipping {len(region_hosts)} host(s).")
            all_missing.extend(region_hosts)
            continue

        print(f"\nRegion '{region}' → {server_ip}  ({len(region_hosts)} host(s))")

        try:
            url, auth = login(server_ip, z_user, z_passwd)
        except Exception as e:
            print(f"  ✗  Login failed: {e}  — skipping region.")
            all_missing.extend(region_hosts)
            continue

        try:
            for hostname in region_hosts:
                matches = wildcard_search(url, auth, hostname)

                if not matches:
                    print(f"  ⚠  '{hostname}' — not found (skipping)")
                    all_missing.append(hostname)
                elif len(matches) == 1:
                    resolved = matches[0]["host"]
                    hid      = matches[0]["hostid"]
                    suffix   = f"  →  {resolved}" if resolved != hostname else ""
                    print(f"  ✓  '{hostname}'{suffix}  (ID: {hid})")
                    all_found.append((url, auth, resolved, hid))
                else:
                    # Multiple matches — list them and skip to avoid accidental deletes
                    print(f"  ⚠  '{hostname}' matched {len(matches)} hosts — ambiguous, skipping:")
                    for m in matches:
                        print(f"       - {m['host']}  (ID: {m['hostid']})")
                    all_missing.append(hostname)
        finally:
            logout(url, auth)

    # 5. Summary before delete
    print(f"\n{'─'*55}")
    print(f"  Found   : {len(all_found)} host(s) to delete")
    print(f"  Missing : {len(all_missing)} host(s) not found / skipped")
    print(f"{'─'*55}")

    if not all_found:
        print("Nothing to delete.")
        return

    if args.dry_run:
        print("\n[Dry run] No changes made.")
        return

    answer = input(f"\nDelete the above {len(all_found)} host(s)? [yes/no]: ").strip().lower()
    if answer not in ("yes", "y"):
        print("Aborted.")
        return

    # 6. Delete in parallel (per-region sessions are already closed; re-login per worker)
    print(f"\nDeleting {len(all_found)} host(s) with {args.workers} parallel workers...\n")

    success = []
    failed  = []
    total   = len(all_found)
    done    = 0
    start   = time.time()

    # Re-login once per unique server for the delete phase
    server_sessions = {}
    for url, _, resolved, hid in all_found:
        if url not in server_sessions:
            try:
                _, auth = login(url.replace("/api_jsonrpc.php", "").replace("http://", ""), z_user, z_passwd)
                server_sessions[url] = auth
            except Exception as e:
                print(f"  ✗  Re-login failed for {url}: {e}")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                delete_one_host,
                url,
                server_sessions.get(url),
                resolved,
                hid,
                args.timeout
            ): resolved
            for url, _, resolved, hid in all_found
            if url in server_sessions
        }

        for future in as_completed(futures):
            resolved, hid, error = future.result()
            done += 1
            elapsed = time.time() - start
            avg     = elapsed / done
            eta     = int(avg * (total - done))

            if error is None:
                success.append(resolved)
                print(f"  ✓  [{done}/{total}]  {resolved}  (ETA: {eta}s)")
            else:
                failed.append((resolved, error))
                print(f"  ✗  [{done}/{total}]  {resolved}  FAILED: {error}")

    # 7. Final summary
    elapsed_total = int(time.time() - start)
    print(f"\n{'─'*55}")
    print(f"  Total time  : {elapsed_total}s")
    print(f"  ✓ Deleted   : {len(success)}/{total}")
    if failed:
        print(f"  ✗ Failed    : {len(failed)}/{total}")
        print(f"\n  Failed hosts:")
        for h, err in failed:
            print(f"    - {h}: {err}")
    if all_missing:
        print(f"\n  ⚠  Not found / skipped ({len(all_missing)}):")
        for h in all_missing:
            print(f"    - {h}")
    print(f"{'─'*55}")

    # 8. Logout all sessions
    for url, auth in server_sessions.items():
        logout(url, auth)
    print("Logged out.")


if __name__ == "__main__":
    main()
