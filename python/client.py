#!/usr/bin/env python3
"""
E2SAR reference client: control-plane setup, sender and worker (reassembler),
combined into a single CLI on top of the e2sar_py bindings.

Setup
-----
Make sure the compiled e2sar_py module is importable, e.g.:

    export PYTHONPATH=/path/to/build/src/pybind

Every subcommand reads the URI from --uri-file (a yaml file holding
'admin_uri'/'instance_uri' keys, default: e2sar.yaml) if it exists,
falling back to the EJFAT_URI environment variable otherwise. --uri
overrides both. `cp reserve`/`cp free`/`cp status` need the *admin*
token; `sender`/`worker` need the *instance* token returned by
`cp reserve`.

Examples
--------
Reserve a load balancer (admin token); both the admin and instance-token
URIs are written to e2sar.yaml, so `cp status`/`cp free` and the
sender/worker processes can all pick up the right one automatically:

    python3 client.py cp reserve --name my-lb --duration 3600

Use a different yaml file for the URI:

    python3 client.py --uri-file lb.yaml cp reserve --name my-lb --duration 3600
    python3 client.py --uri-file lb.yaml cp status

Check load balancer / worker status (admin token):

    python3 client.py cp status

Free a previously reserved load balancer (admin token):

    python3 client.py cp free

Run a sender that segments and streams events to the load balancer:

    python3 client.py sender --data-id 1 --event-src-id 1 --size 65536 --rate 1.0

Run a worker that registers with the control plane and reassembles events:

    python3 client.py worker --node-name worker1 --port 20000 --threads 2
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime

import yaml

import e2sar_py

IP_FAMILY = {"dual": 0, "ipv4": 1, "ipv6": 2}


def _log(msg, **kwargs):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')}] {msg}", **kwargs)


def _die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _unwrap(res, what):
    if res.has_error():
        _die(f"{what}: {res.error().message()}")
    return res.value()


# cp reserve/free/status need the admin token; sender/worker need the instance
# token. A single URI string can only carry one token, so the yaml file keeps
# both under separate keys.
_TOKEN_TYPE_KEYS = {
    e2sar_py.EjfatURI.TokenType.admin: "admin_uri",
    e2sar_py.EjfatURI.TokenType.instance: "instance_uri",
}


def _save_uri_to_yaml(path, uri):
    doc = {}
    if not uri.get_admin_token().has_error():
        doc["admin_uri"] = uri.to_string(e2sar_py.EjfatURI.TokenType.admin)
    if not uri.get_instance_token().has_error():
        doc["instance_uri"] = uri.to_string(e2sar_py.EjfatURI.TokenType.instance)
    with open(path, "w") as f:
        yaml.safe_dump(doc, f)


def _load_uri_from_yaml(path, token_type):
    key = _TOKEN_TYPE_KEYS[token_type]
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    uri_str = doc.get(key)
    if not uri_str:
        _die(f"no '{key}' key found in {path}")
    return e2sar_py.EjfatURI(uri=uri_str, tt=token_type)


def _redact_uri(uri_str):
    return re.sub(r"(://)(.{4})[^@]*(.{4})@", r"\1\2---\3@", uri_str)


def _load_uri(args, token_type):
    if args.uri:
        source = "--uri"
        uri = e2sar_py.EjfatURI(uri=args.uri, tt=token_type)
    elif args.uri_file and os.path.exists(args.uri_file):
        source = args.uri_file
        uri = _load_uri_from_yaml(args.uri_file, token_type)
    else:
        source = "EJFAT_URI environment variable"
        res = e2sar_py.EjfatURI.get_from_env(tt=token_type)
        if res.has_error():
            _die(f"reading EJFAT_URI: {res.error().message()}")
        uri = res.value()

    print(f"Loading EJFAT_URI from {source}...")
    print(f"EJFAT_URI: {_redact_uri(uri.to_string(token_type))}")
    return uri


# ------------------------------------------------------------- control plane

def cmd_cp_reserve(args):
    uri = _load_uri(args, e2sar_py.EjfatURI.TokenType.admin)
    lbm = e2sar_py.ControlPlane.LBManager(uri, not args.insecure)

    senders = args.senders.split(",") if args.senders else []
    fpga_id = _unwrap(
        lbm.reserve_lb_in_seconds(
            lb_id=args.name,
            seconds=float(args.duration),
            senders=senders,
            ip_family=IP_FAMILY[args.ip_family],
        ),
        "reserving load balancer",
    )

    print(f"Reserved load balancer '{args.name}' (fpga id {fpga_id})")
    uri = lbm.get_uri()
    if args.uri_file:
        _save_uri_to_yaml(args.uri_file, uri)
        print(f"Admin and instance-token URIs written to {args.uri_file}")
    else:
        print("Instance-token URI - export this as EJFAT_URI for the sender/worker:")
        print(uri.to_string(e2sar_py.EjfatURI.TokenType.instance))


def cmd_cp_free(args):
    uri = _load_uri(args, e2sar_py.EjfatURI.TokenType.admin)
    lbm = e2sar_py.ControlPlane.LBManager(uri, not args.insecure)

    print("Freeing a load balancer")
    print(
        f"   Contacting: {_redact_uri(uri.to_string(e2sar_py.EjfatURI.TokenType.admin))} "
        f"using address: {lbm.get_addr_string()}"
    )
    print(f"   LB ID: {uri.lb_id}")

    _unwrap(lbm.free_lb(), "freeing load balancer")
    print("Success.")
    print("Reservation freed successfully")


def cmd_cp_status(args):
    uri = _load_uri(args, e2sar_py.EjfatURI.TokenType.admin)
    lbm = e2sar_py.ControlPlane.LBManager(uri, not args.insecure)

    print("Getting LB Status")
    print(
        f"   Contacting: {uri.to_string(e2sar_py.EjfatURI.TokenType.session)} "
        f"using address: {lbm.get_addr_string()}"
    )
    print(f"   LB ID: {uri.lb_id}")

    status = lbm.get_lb_status()
    if status is None:
        _die("fetching load balancer status")

    lb_status = e2sar_py.ControlPlane.LBManager.as_lb_status(status)
    print(
        f"LB details: expiresat={lb_status.expiresAt}, currentepoch={lb_status.currentEpoch}, "
        f"predictedeventnum={lb_status.currentPredictedEventNumber}"
    )

    print(f"Registered senders: {' '.join(lbm.get_sender_addresses(status))}")

    print("Registered workers:")
    workers = lbm.get_worker_statuses(status)
    if not workers:
        print("  (none)")
    for w in workers:
        print(
            f"  worker={w.get_name()} fill={w.get_fill_percent():.2f} "
            f"signal={w.get_control_signal():.2f} slots={w.get_slots_assigned()} "
            f"updated={w.get_last_updated()}"
        )


# ------------------------------------------------------------------- sender

def cmd_sender(args):
    uri = _load_uri(args, e2sar_py.EjfatURI.TokenType.instance)

    sflags = e2sar_py.DataPlane.Segmenter.SegmenterFlags()
    sflags.useCP = not args.no_cp
    sflags.rateGbps = args.rate
    sflags.mtu = args.mtu

    _log(f"E2SAR Selected Optimizations:  {' '.join(e2sar_py.Optimizations.selectedAsStrings())}")

    lbm = None
    if sflags.useCP:
        lbm = e2sar_py.ControlPlane.LBManager(uri, not args.insecure)
        _log("Adding senders to LB: autodetected ... ", end="", flush=True)
        _unwrap(lbm.add_sender_self(False), "adding sender to LB")
        print("done")

    _log(f"Control plane:                 {'ON' if sflags.useCP else 'OFF'}")
    _log(f"Sending sockets/threads:       {sflags.numSendSockets}")
    if sflags.rateGbps > 0:
        _log(f"Sending average bit rate is:   {sflags.rateGbps} Gbps (with {args.size} B line-rate bursts)")
        _log(f"Inter-event sleep (usec) is:   {int(args.size * 8 / (sflags.rateGbps * 1000))}")
    else:
        _log("Sending average bit rate is:   unlimited")
    _log(
        "*** Make sure the LB has been reserved and the URI reflects the reserved instance information."
        if sflags.useCP
        else "*** Make sure the URI reflects proper data address, other parts are ignored."
    )
    _log(f"Event size is {args.size} bytes or {args.size * 8} bits")
    _log(f"Sending {args.count} event buffers" if args.count > 0 else "Sending events until Ctrl-C")

    seg = e2sar_py.DataPlane.Segmenter(uri, args.data_id, args.event_src_id, sflags)
    _unwrap(seg.OpenAndStart(), "starting segmenter")
    _log(f"Using MTU {seg.getMTU()}")

    payload = os.urandom(args.size)
    sent = 0
    start = time.perf_counter()
    try:
        while args.count <= 0 or sent < args.count:
            _unwrap(seg.sendEvent(payload, len(payload)), "sending event")
            sent += 1
            if sent % 10 == 0:
                stats = seg.getSendStats()
                _log(f"sent {sent} events, {stats.msgCnt} fragments, {stats.errCnt} errors")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.perf_counter() - start
        _log("Stopping threads")
        seg.stopThreads()

        stats = seg.getSendStats()
        _log(f"Completed, {stats.msgCnt} packets sent, {stats.errCnt} errors")
        _log(f"Elapsed usecs: {int(elapsed * 1_000_000)} microseconds")
        if elapsed > 0:
            effective_gbps = stats.msgCnt * seg.getMTU() * 8 / elapsed / 1e9
            goodput_gbps = sent * args.size * 8 / elapsed / 1e9
            _log(f"Estimated effective throughput (Gbps): {effective_gbps:.6f}")
            _log(f"Estimated goodput (Gbps): {goodput_gbps:.6f}")

        if lbm is not None:
            _log("Removing senders: self")
            _unwrap(lbm.remove_sender_self(False), "removing sender from LB")


# ------------------------------------------------------------------- worker

def _print_worker_stats(stats):
    _log("Stats:")
    print(f"\tTotal Bytes: {stats.totalBytes}")
    print(f"\tTotal Packets: {stats.totalPackets}")
    print(f"\tBad RE Header Discards: {stats.badHeaderDiscards}")
    print(f"\tEvents Received: {stats.eventSuccess}")
    print(f"\tEvents Lost in reassembly: {stats.reassemblyLoss}")
    print(f"\tEvents Lost in enqueue: {stats.enqueueLoss}")
    print(f"\tData Errors: {stats.dataErrCnt}")
    print(f"\tgRPC Errors: {stats.grpcErrCnt}")


def cmd_worker(args):
    uri = _load_uri(args, e2sar_py.EjfatURI.TokenType.instance)

    rflags = e2sar_py.DataPlane.Reassembler.ReassemblerFlags()
    rflags.useCP = not args.no_cp
    rflags.weight = args.weight

    cp_host, _ = _unwrap(uri.get_cp_host(), "reading control plane host")
    cp_addr, _ = _unwrap(uri.get_cp_addr(), "reading control plane address")
    _log(f"LB Host: {cp_host}")
    _log(f"LB IP: {cp_addr}")

    if args.data_ip:
        receiver_ip = args.data_ip
        data_ip = e2sar_py.IPAddress.from_string(args.data_ip)
        reas = e2sar_py.DataPlane.Reassembler(uri, data_ip, args.port, args.threads, rflags)
    else:
        _log("Auto-detecting receiver IP...")
        # auto-detect the outgoing interface towards the load balancer
        receiver_ip = uri.get_dp_local_addrs()[0]
        reas = e2sar_py.DataPlane.Reassembler(uri, args.port, args.threads, rflags)
    _log(f"Receiver IP: {receiver_ip}")
    _log(f"Data Port: {args.port}")
    _log(f"Receive Threads: {args.threads}")
    _log(f"Buffer Size: {rflags.rcvSocketBufSize}")

    if rflags.useCP:
        _unwrap(reas.registerWorker(args.node_name), "registering worker")

    _unwrap(reas.OpenAndStart(), "starting reassembler")
    _log(f"Running: worker '{args.node_name}' ip={receiver_ip} port={args.port} threads={args.threads}")

    received = 0
    try:
        while True:
            recv_len, recv_bytes, event_num, data_id = reas.recvEventBytes(wait_ms=200)
            if recv_len == -2:
                _log("receive error, continuing")
                continue
            if recv_len == -1:
                continue
            received += 1
            _log(f"received event #{event_num} data_id={data_id} bytes={recv_len}")
            if received % 50 == 0:
                _print_worker_stats(reas.getStats())
    except KeyboardInterrupt:
        pass
    finally:
        if rflags.useCP:
            reas.deregisterWorker()
        reas.stopThreads()
        _log(f"Worker stopped after receiving {received} events")
        _print_worker_stats(reas.getStats())


# ---------------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uri", default=None, help="EJFAT URI (default: read EJFAT_URI env var)")
    parser.add_argument(
        "--uri-file",
        default="e2sar.yaml",
        help="path to a yaml file holding 'admin_uri'/'instance_uri' keys; "
        "'cp reserve' writes both here, other commands read the one they need from here "
        "(--uri takes precedence over this, which takes precedence over EJFAT_URI) "
        "(default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cp = sub.add_parser("cp", help="control plane setup")
    cp_sub = cp.add_subparsers(dest="cp_command", required=True)

    cp_reserve = cp_sub.add_parser("reserve", help="reserve a load balancer")
    cp_reserve.add_argument("--name", required=True, help="load balancer name")
    cp_reserve.add_argument("--duration", type=float, default=3600, help="reservation length in seconds")
    cp_reserve.add_argument("--senders", default=None, help="comma-separated list of sender IPs")
    cp_reserve.add_argument("--ip-family", choices=IP_FAMILY, default="dual")
    cp_reserve.add_argument("--insecure", action="store_true", help="skip TLS certificate validation")
    cp_reserve.set_defaults(func=cmd_cp_reserve)

    cp_free = cp_sub.add_parser("free", help="free a reserved load balancer")
    cp_free.add_argument("--insecure", action="store_true", help="skip TLS certificate validation")
    cp_free.set_defaults(func=cmd_cp_free)

    cp_status = cp_sub.add_parser("status", help="show load balancer / worker status")
    cp_status.add_argument("--insecure", action="store_true", help="skip TLS certificate validation")
    cp_status.set_defaults(func=cmd_cp_status)

    sender = sub.add_parser("sender", help="segment and send events")
    sender.add_argument("--data-id", type=int, default=0, help="data source identifier")
    sender.add_argument("--event-src-id", type=int, default=0, help="event source identifier")
    sender.add_argument("--size", type=int, default=1024, help="event size in bytes")
    sender.add_argument("--count", type=int, default=0, help="number of events to send (0 = until Ctrl-C)")
    sender.add_argument("--interval", type=float, default=1.0, help="seconds between events")
    sender.add_argument("--rate", type=float, default=-1.0, help="send rate in Gbps (negative = unlimited)")
    sender.add_argument("--mtu", type=int, default=1500, help="MTU used for segmentation")
    sender.add_argument("--no-cp", action="store_true", help="disable control plane sync packets")
    sender.add_argument("--insecure", action="store_true", help="skip TLS certificate validation")
    sender.set_defaults(func=cmd_sender)

    worker = sub.add_parser("worker", help="register and reassemble events")
    worker.add_argument("--node-name", required=True, help="worker name used in control plane registration")
    worker.add_argument("--data-ip", default=None, help="IP to listen on (default: auto-detect)")
    worker.add_argument("--port", type=int, default=10000, help="starting UDP port to listen on")
    worker.add_argument("--threads", type=int, default=1, help="number of receive threads")
    worker.add_argument("--weight", type=float, default=1.0, help="worker weight for slot assignment")
    worker.add_argument("--no-cp", action="store_true", help="disable control plane registration")
    worker.set_defaults(func=cmd_worker)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
