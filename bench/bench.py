#!/usr/bin/env python3
"""
request/response latency benchmark for a SOCKS5 proxy.

it simulates the trading setup: a "gateway" keeping a constant request rate
with a maximum number of in-flight transactions, connected through the proxy
to a "market" that processes at most CAPACITY requests per second.

every message carries 4 timestamps (same monotonic clock, same host):
  gateway send -> market in -> market out -> gateway receive
so the delay of each leg and the in-flight count seen by the gateway can be
compared with the one actually seen by the market. the difference is what is
travelling through network + proxy.

usually started through bench/run.sh, which sets up the network delays.
"""
import argparse, collections, os, select, socket, struct, sys, time

HDR = struct.Struct('<Qdddd')  # seq, t_send, t_mkt_in, t_mkt_out, unused

def addr(s):
    h, p = s.rsplit(':', 1)
    return h, int(p)

def market(listen, capacity, size):
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(listen)
    ls.listen(1)
    c, _ = ls.accept()
    ls.close()
    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    service = 1.0 / capacity
    next_free = 0.0
    queue = collections.deque()  # (due, bytearray)
    buf = b''
    while True:
        to = max(0.0, queue[0][0] - time.perf_counter()) if queue else None
        if select.select([c], [], [], to)[0]:
            d = c.recv(65536)
            if not d:
                os._exit(0)
            now = time.perf_counter()
            buf += d
            while len(buf) >= size:
                m = bytearray(buf[:size])
                buf = buf[size:]
                struct.pack_into('<d', m, 16, now)
                # one request at a time, CAPACITY per second
                next_free = max(now, next_free) + service
                queue.append((next_free, m))
        now = time.perf_counter()
        out = bytearray()
        while queue and queue[0][0] <= now:
            m = queue.popleft()[1]
            struct.pack_into('<d', m, 24, now)
            out += m
        if out:
            c.sendall(out)

def socks_connect(proxy, target):
    s = socket.create_connection(proxy)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.sendall(b'\x05\x01\x00')
    if s.recv(2) != b'\x05\x00':
        sys.exit('socks auth failed')
    s.sendall(b'\x05\x01\x00\x01' + socket.inet_aton(target[0]) + struct.pack('>H', target[1]))
    r = s.recv(10)
    if len(r) < 2 or r[1] != 0:
        sys.exit('socks connect failed')
    return s

def gateway(s, rate, window, duration, size):
    recs = []
    pad = b'x' * (size - HDR.size)
    interval = 1.0 / rate
    t0 = time.perf_counter()
    next_send = t0
    sent = 0
    buf = b''
    while True:
        now = time.perf_counter()
        sending = now < t0 + duration
        if not sending and len(recs) == sent:
            break
        # keep the rate; when the window is full wait, without bursting later
        while sending and now >= next_send and sent - len(recs) < window:
            s.sendall(HDR.pack(sent, time.perf_counter(), 0, 0, 0) + pad)
            sent += 1
            next_send += interval
        if sending and now >= next_send:
            next_send = now
        to = max(0.0, next_send - now) if sending and sent - len(recs) < window else 1.0
        if select.select([s], [], [], to)[0]:
            d = s.recv(65536)
            if not d:
                sys.exit('connection closed')
            now = time.perf_counter()
            buf += d
            while len(buf) >= size:
                _, ts, tin, tout, _ = HDR.unpack_from(buf)
                recs.append((ts, tin, tout, now))
                buf = buf[size:]
    return recs

def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))]

def inflight(intervals, t_from, t_to):
    """time weighted average and max of the overlapping intervals"""
    ev = sorted([(a, 1) for a, b in intervals] + [(b, -1) for a, b in intervals])
    cur = peak = 0
    area = 0.0
    last = t_from
    for t, d in ev:
        if t > t_from:
            tt = min(t, t_to)
            area += cur * (tt - max(last, t_from))
            last = tt
        cur += d
        if t_from <= t <= t_to:
            peak = max(peak, cur)
    return area / (t_to - t_from), peak

COLS = ('binary', 'tps', 'e2e p50', 'e2e p99', 'e2e max', 'req p50', 'req p99',
        'resp p50', 'resp p99', 'fly gw', 'fly mkt', 'in transit', 'max gw', 'max mkt')
FMT = '%-10s %5s %8s %8s %8s %8s %8s %8s %8s %7s %7s %10s %6s %7s'

def report(label, recs, warmup):
    t_start = recs[0][0] + warmup
    t_end = recs[-1][0]
    r = [x for x in recs if t_start <= x[0] <= t_end]
    ms = lambda v: '%.2f' % (v * 1000)
    e2e = [d - a for a, b, c, d in r]
    req = [b - a for a, b, c, d in r]
    resp = [d - c for a, b, c, d in r]
    fgw, mgw = inflight([(a, d) for a, b, c, d in recs], t_start, t_end)
    fmk, mmk = inflight([(b, c) for a, b, c, d in recs], t_start, t_end)
    tps = sum(1 for x in recs if t_start <= x[3] <= t_end) / (t_end - t_start)
    print(FMT % (label, '%.0f' % tps, ms(pct(e2e, .5)), ms(pct(e2e, .99)), ms(max(e2e)),
                 ms(pct(req, .5)), ms(pct(req, .99)), ms(pct(resp, .5)), ms(pct(resp, .99)),
                 '%.1f' % fgw, '%.1f' % fmk, '%.1f' % (fgw - fmk), mgw, mmk))
    sys.stdout.flush()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--proxy', type=addr, default='127.0.0.1:1080')
    ap.add_argument('--market', type=addr, default='127.0.0.2:9000')
    ap.add_argument('--label', default='proxy')
    ap.add_argument('--rate', type=float, default=400, help='gateway requests/s (default 400)')
    ap.add_argument('--capacity', type=float, default=400, help='market requests/s (default 400)')
    ap.add_argument('--window', type=int, default=300, help='max in-flight at gateway (default 300)')
    ap.add_argument('--duration', type=float, default=20, help='seconds (default 20)')
    ap.add_argument('--warmup', type=float, default=1, help='seconds ignored at start (default 1)')
    ap.add_argument('--size', type=int, default=120, help='message size in bytes (default 120)')
    ap.add_argument('--header', action='store_true', help='print the column header')
    a = ap.parse_args()
    if a.size < HDR.size:
        sys.exit('size must be >= %d' % HDR.size)
    if a.header:
        print(FMT % COLS)
        print('(times in ms; req = gateway->market leg, resp = market->gateway leg;'
              ' fly = time-weighted avg in-flight; in transit = fly gw - fly mkt)')
    pid = os.fork()
    if pid == 0:
        market(a.market, a.capacity, a.size)
    time.sleep(0.2)
    s = socks_connect(a.proxy, a.market)
    recs = gateway(s, a.rate, a.window, a.duration, a.size)
    s.close()
    os.waitpid(pid, 0)
    report(a.label, recs, a.warmup)

if __name__ == '__main__':
    main()
