#!/usr/bin/env python3
"""Bounded TCP connection load tester for LDAP/LDAPS — runs from outside the
container, against any host:port you pass as arguments.

LDAP isn't HTTP, so this doesn't send GET requests like flood.py. Instead it
opens plain TCP connections (the same thing an LDAP client does before any
bind/search), which is exactly what the ldap-flood Fail2ban filter watches
for in slapd's log (one ACCEPT line per new connection)."""

from __future__ import annotations

import argparse
import socket
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

CONNECT_TIMEOUT_SECONDS = 2
REPORT_INTERVAL_SECONDS = 3
MAX_DURATION_SECONDS = 300
MAX_CONCURRENCY = 500
MAX_CONNECTIONS_PER_SECOND = 500


def bounded_int(name: str, value: int, maximum: int) -> int:
    if not 1 <= value <= maximum:
        raise SystemExit(f"--{name} must be between 1 and {maximum}; got {value}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bounded TCP connection load test against an LDAP/LDAPS host:port."
    )
    parser.add_argument("host", help="Target host or IP, e.g. localhost or 127.0.0.1")
    parser.add_argument("port", type=int, help="Target port, e.g. 389 or 636")
    parser.add_argument(
        "--duration", type=int, default=20, help="Seconds to run (max 300, default 20)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=40, help="Parallel workers (max 500, default 40)"
    )
    parser.add_argument(
        "--cps",
        type=int,
        default=50,
        help="Max aggregate connections/second (max 500, default 50)",
    )
    args = parser.parse_args()

    bounded_int("duration", args.duration, MAX_DURATION_SECONDS)
    bounded_int("concurrency", args.concurrency, MAX_CONCURRENCY)
    bounded_int("cps", args.cps, MAX_CONNECTIONS_PER_SECOND)
    return args


def connect_once(host: str, port: int) -> str:
    """Open and immediately close one TCP connection. Return its outcome."""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS):
            return "connected"
    except ConnectionRefusedError:
        return "refused"
    except (TimeoutError, socket.timeout):
        return "timeout"
    except OSError:
        return "error"


class RateLimiter:
    def __init__(self, connections_per_second: int) -> None:
        self.interval = 1 / connections_per_second
        self.next_slot_at = 0.0
        self.lock = threading.Lock()

    def wait_for_turn(self, stop_event: threading.Event) -> bool:
        with self.lock:
            now = time.monotonic()
            scheduled_at = max(now, self.next_slot_at)
            self.next_slot_at = scheduled_at + self.interval
        return not stop_event.wait(max(0.0, scheduled_at - now))


def worker(
    host: str,
    port: int,
    deadline: float,
    stop_event: threading.Event,
    rate_limiter: RateLimiter,
    counts: Counter[str],
    counts_lock: threading.Lock,
) -> None:
    while not stop_event.is_set() and time.monotonic() < deadline:
        if not rate_limiter.wait_for_turn(stop_event):
            break
        if time.monotonic() >= deadline:
            break
        outcome = connect_once(host, port)
        with counts_lock:
            counts[outcome] += 1


def snapshot(counts: Counter[str], counts_lock: threading.Lock) -> Counter[str]:
    with counts_lock:
        return counts.copy()


def report(target: str, counts: Counter[str], final: bool = False) -> None:
    total = sum(counts.values())
    connected = counts["connected"]
    blocked = total - connected
    tag = "RESULT" if final else f"[{datetime.now():%H:%M:%S}] connections so far"
    print(
        f"  {tag} for {target}: total={total} connected={connected} "
        f"refused/timeout={blocked}",
        flush=True,
    )
    if final and total:
        print(f"  -> {blocked * 100 // total}% of connection attempts were refused/timed out", flush=True)


def main() -> None:
    args = parse_args()
    target = f"{args.host}:{args.port}"

    print(
        f"=== Bounded TCP connection load test against {target} (LDAP/LDAPS) ===\n"
        f"    {args.concurrency} workers, up to {args.cps} connections/s, "
        f"{args.duration}s\n",
        flush=True,
    )

    counts: Counter[str] = Counter()
    counts_lock = threading.Lock()
    stop_event = threading.Event()
    rate_limiter = RateLimiter(args.cps)
    deadline = time.monotonic() + args.duration
    interrupted = False

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                worker,
                args.host,
                args.port,
                deadline,
                stop_event,
                rate_limiter,
                counts,
                counts_lock,
            )
            for _ in range(args.concurrency)
        ]
        try:
            while time.monotonic() < deadline:
                time.sleep(min(REPORT_INTERVAL_SECONDS, deadline - time.monotonic()))
                report(target, snapshot(counts, counts_lock))
        except KeyboardInterrupt:
            interrupted = True
            stop_event.set()
            print("\nStopping workers after Ctrl-C...", flush=True)
        finally:
            stop_event.set()

        for future in futures:
            future.result()

    report(target, snapshot(counts, counts_lock), final=True)
    if interrupted:
        print("\n=== Test interrupted. ===")
    else:
        print("\n=== Test finished. ===")
    print("A refused/timeout result can be a Fail2ban ban or the server dropping new connections.")


if __name__ == "__main__":
    main()
