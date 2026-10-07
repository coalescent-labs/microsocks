#!/usr/bin/env python3
"""
functional and resource regression tests for microsocks.

usage: tests/run.sh BINARY [BINARY ...]
       (runs this script in a private network namespace)

every test starts its own proxy instance. set WRAP to run the binary through
a wrapper, e.g. WRAP="valgrind --leak-check=full --log-file=vg.%p".
QUICK=1 reduces the data volumes (useful under valgrind).
ONLY=name1,name2 runs only the given tests.
python 3.6 compatible, so it also runs on RHEL 8.
"""
import hashlib, os, shlex, socket, struct, subprocess, sys, tempfile, threading, time

QUICK = os.environ.get('QUICK') == '1'
WRAP = shlex.split(os.environ.get('WRAP', ''))
TMO = 30 if WRAP else 5
_port = [21000]

def recvn(s, n):
    b = b''
    while len(b) < n:
        d = s.recv(n - len(b))
        if not d:
            break
        b += d
    return b

def is_closed(s, wait):
    """True if the peer closes the connection within wait seconds"""
    s.settimeout(wait)
    try:
        while True:
            d = s.recv(65536)
            if not d:
                return True
    except socket.timeout:
        return False
    except ConnectionResetError:
        return True
    finally:
        s.settimeout(TMO)

PROXIES = []

class Proxy:
    def __init__(self, binary, *args):
        _port[0] += 1
        self.port = _port[0]
        self.log = tempfile.TemporaryFile()
        self.p = subprocess.Popen(WRAP + [binary, '-i', '127.0.0.1', '-p', str(self.port)] + list(args),
                                  stderr=self.log)
        PROXIES.append(self)
        for _ in range(300):
            try:
                socket.create_connection(('127.0.0.1', self.port)).close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError('proxy did not start')

    def stats(self):
        st = {}
        with open('/proc/%d/status' % self.p.pid) as f:
            for l in f:
                k, v = l.split(':', 1)
                if k in ('VmRSS', 'Threads'):
                    st[k] = int(v.split()[0])
        st['fds'] = len(os.listdir('/proc/%d/fd' % self.p.pid))
        return st

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(TMO)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()

class Server(threading.Thread):
    def __init__(self, handler, host='127.0.0.1'):
        threading.Thread.__init__(self, daemon=True)
        self.s = socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if host == '::':
            self.s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        self.s.bind((host, 0))
        self.s.listen(1024)
        self.port = self.s.getsockname()[1]
        self.handler = handler
        self.start()

    def run(self):
        while True:
            c, _ = self.s.accept()
            threading.Thread(target=self.handler, args=(c,), daemon=True).start()

def h_echo(c):
    try:
        while True:
            d = c.recv(65536)
            if not d:
                break
            c.sendall(d)
    except OSError:
        pass
    c.close()

def h_whoami(c):
    c.sendall(c.getpeername()[0].encode())
    c.close()

BULK = os.urandom((2 if os.environ.get('QUICK') == '1' else 16) << 20)

def h_bulk(c):
    """send a lot of data and close immediately"""
    c.sendall(BULK)
    c.close()

FLOOD_RX = {}

def h_flood(c):
    """flood the client and count what the client sends us"""
    port = c.getpeername()[1]
    FLOOD_RX[port] = 0
    def tx():
        chunk = b'x' * 65536
        try:
            while True:
                c.sendall(chunk)
        except OSError:
            pass
    threading.Thread(target=tx, daemon=True).start()
    try:
        while True:
            d = c.recv(65536)
            if not d:
                break
            FLOOD_RX[port] += len(d)
    except OSError:
        pass
    c.close()

def h_noread(c):
    """accept and never read"""
    time.sleep(3600)

def greet(port, methods=b'\x00'):
    s = socket.create_connection(('127.0.0.1', port))
    s.settimeout(TMO)
    s.sendall(b'\x05' + bytes([len(methods)]) + methods)
    return s, recvn(s, 2)

def auth(s, user, pw):
    s.sendall(b'\x01' + bytes([len(user)]) + user + bytes([len(pw)]) + pw)
    return recvn(s, 2)

def request(s, host, port, cmd=1):
    try:
        a = b'\x01' + socket.inet_pton(socket.AF_INET, host)
    except OSError:
        try:
            a = b'\x04' + socket.inet_pton(socket.AF_INET6, host)
        except OSError:
            a = b'\x03' + bytes([len(host)]) + host.encode()
    s.sendall(b'\x05' + bytes([cmd]) + b'\x00' + a + struct.pack('>H', port))
    r = recvn(s, 10)
    return r[1] if len(r) == 10 and r[0] == 5 else None

def connect(proxy, host, port):
    s, r = greet(proxy.port)
    assert r == b'\x05\x00', r
    rc = request(s, host, port)
    return s, rc

def roundtrip(s, data=b'ping'):
    s.sendall(data)
    return recvn(s, len(data)) == data

# ---------------------------------------------------------------- tests

def t_connect_ipv4(b):
    p = Proxy(b)
    s, rc = connect(p, '127.0.0.1', ECHO.port)
    ok = rc == 0 and roundtrip(s, b'hello')
    p.stop()
    return ok

def t_connect_dns(b):
    p = Proxy(b, '-q')
    s, rc = connect(p, 'localhost', DUAL.port)
    ok = rc == 0 and roundtrip(s)
    p.stop()
    return ok

def t_connect_ipv6(b):
    p = Proxy(b, '-q')
    s, rc = connect(p, '::1', ECHO6.port)
    ok = rc == 0 and roundtrip(s)
    p.stop()
    return ok

def t_err_refused(b):
    p = Proxy(b, '-q')
    s, rc = connect(p, '127.0.0.1', 1)
    ok = rc == 5 and is_closed(s, TMO)
    p.stop()
    return ok

def t_err_unresolvable(b):
    p = Proxy(b, '-q')
    s, rc = connect(p, 'nonexistent.invalid', 80)
    ok = rc == 1 and is_closed(s, TMO)
    p.stop()
    return ok

def t_err_bad_command(b):
    p = Proxy(b, '-q')
    s, _ = greet(p.port)
    ok = request(s, '127.0.0.1', ECHO.port, cmd=2) == 7
    p.stop()
    return ok

def t_err_bad_atyp(b):
    p = Proxy(b, '-q')
    s, _ = greet(p.port)
    s.sendall(b'\x05\x01\x00\x09\x7f\x00\x00\x01\x00\x50')
    r = recvn(s, 10)
    p.stop()
    return len(r) == 10 and r[1] == 8

def t_err_bad_version(b):
    p = Proxy(b, '-q')
    s = socket.create_connection(('127.0.0.1', p.port))
    s.settimeout(TMO)
    s.sendall(b'\x04\x01\x00\x50\x7f\x00\x00\x01\x00')
    ok = recvn(s, 2) == b'\x05\xff' and is_closed(s, TMO)
    p.stop()
    return ok

def t_auth_required(b):
    p = Proxy(b, '-q', '-u', 'user', '-P', 'secret')
    s, r = greet(p.port, b'\x00')
    ok = r == b'\x05\xff' and is_closed(s, TMO)
    p.stop()
    return ok

def t_auth_ok(b):
    p = Proxy(b, '-q', '-u', 'user', '-P', 'secret')
    s, r = greet(p.port, b'\x00\x02')
    ok = r == b'\x05\x02' and auth(s, b'user', b'secret') == b'\x01\x00'
    ok = ok and request(s, '127.0.0.1', ECHO.port) == 0 and roundtrip(s)
    p.stop()
    return ok

def t_auth_wrong(b):
    p = Proxy(b, '-q', '-u', 'user', '-P', 'secret')
    s, r = greet(p.port, b'\x02')
    a = auth(s, b'user', b'wrong')
    ok = r == b'\x05\x02' and len(a) == 2 and a[1] != 0 and is_closed(s, TMO)
    p.stop()
    return ok

def t_whitelist(b):
    p = Proxy(b, '-q', '-u', 'user', '-P', 'secret', '-w', '127.0.0.1')
    s, rc = connect(p, '127.0.0.1', ECHO.port)
    ok = rc == 0 and roundtrip(s)
    p.stop()
    return ok

def t_auth_once(b):
    p = Proxy(b, '-q', '-1', '-u', 'user', '-P', 'secret')
    s, r = greet(p.port, b'\x00')
    ok = r == b'\x05\xff'
    s, r = greet(p.port, b'\x02')
    ok = ok and auth(s, b'user', b'secret') == b'\x01\x00'
    s, rc = connect(p, '127.0.0.1', ECHO.port)
    ok = ok and rc == 0 and roundtrip(s)
    p.stop()
    return ok

def t_bind_addr(b):
    p = Proxy(b, '-q', '-b', '127.0.0.3')
    s, rc = connect(p, '127.0.0.1', WHOAMI.port)
    ok = rc == 0 and recvn(s, 64) == b'127.0.0.3'
    p.stop()
    return ok

def t_full_duplex_integrity(b):
    """same amount of data in both directions at the same time"""
    n = (4 if QUICK else 64) << 20
    p = Proxy(b, '-q')
    s, rc = connect(p, '127.0.0.1', ECHO.port)
    data = os.urandom(n)
    def tx():
        s.sendall(data)
    t = threading.Thread(target=tx, daemon=True)
    t.start()
    got = hashlib.sha256()
    left = n
    while left:
        d = s.recv(min(left, 1 << 20))
        if not d:
            break
        got.update(d)
        left -= len(d)
    t.join(TMO)
    p.stop()
    return rc == 0 and left == 0 and got.digest() == hashlib.sha256(data).digest()

def t_target_bulk_then_close(b):
    """nothing buffered in the proxy is lost when the target closes"""
    p = Proxy(b, '-q')
    s, rc = connect(p, '127.0.0.1', BULK_SRV.port)
    time.sleep(0.5)  # let the target finish and close while we are not reading
    got = recvn(s, len(BULK) + 1)
    p.stop()
    return rc == 0 and got == BULK

def t_directions_independent(b):
    """client stops reading: requests must still reach the target"""
    p = Proxy(b, '-q')
    s, rc = connect(p, '127.0.0.1', FLOOD.port)
    time.sleep(1)  # all buffers towards the client fill up
    sent = 256 << 10
    s.sendall(b'r' * sent)
    t0 = time.time()
    while time.time() - t0 < 3 and sum(FLOOD_RX.values()) < sent:
        time.sleep(0.05)
    got = sum(FLOOD_RX.values())
    FLOOD_RX.clear()
    INFO.append('    target received %d of %d bytes' % (got, sent))
    p.stop()
    return rc == 0 and got == sent

def t_no_spin_on_reset(b):
    """client resets while the target is not reading: no busy loop"""
    p = Proxy(b, '-q')
    s, rc = connect(p, '127.0.0.1', NOREAD.port)
    s.setblocking(False)
    last = time.time()
    while time.time() - last < 1:  # until every buffer towards the target is full
        try:
            s.send(b'x' * 65536)
            last = time.time()
        except BlockingIOError:
            time.sleep(0.01)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    s.close()  # RST
    time.sleep(0.5)
    def cpu():
        with open('/proc/%d/stat' % p.p.pid) as f:
            v = f.read().rsplit(')', 1)[1].split()
        return (int(v[11]) + int(v[12])) / os.sysconf('SC_CLK_TCK')
    c0 = cpu()
    time.sleep(2)
    used = cpu() - c0
    INFO.append('    proxy cpu time in 2s after reset: %.2fs' % used)
    p.stop()
    return rc == 0 and used < 0.2

def t_eof_from_client(b):
    p = Proxy(b, '-q')
    s, rc = connect(p, '127.0.0.1', ECHO.port)
    ok = rc == 0 and roundtrip(s)
    s.shutdown(socket.SHUT_WR)
    ok = ok and is_closed(s, TMO)
    p.stop()
    return ok

def t_eof_from_target(b):
    p = Proxy(b, '-q')
    s, rc = connect(p, '127.0.0.1', WHOAMI.port)
    ok = rc == 0 and recvn(s, 64) == b'127.0.0.1' and is_closed(s, TMO)
    p.stop()
    return ok

def t_idle_kept(b):
    p = Proxy(b, '-q')
    s, rc = connect(p, '127.0.0.1', ECHO.port)
    time.sleep(3)
    ok = rc == 0 and roundtrip(s)
    p.stop()
    return ok

def t_idle_timeout(b):
    p = Proxy(b, '-q', '-t', '1')
    s, rc = connect(p, '127.0.0.1', ECHO.port)
    ok = rc == 0 and roundtrip(s) and is_closed(s, 4)
    p.stop()
    return ok

def t_concurrency(b):
    """many simultaneous connections exchanging data"""
    nconn, rounds = (50, 5) if QUICK else (300, 20)
    p = Proxy(b, '-q')
    res = []
    def worker(i):
        try:
            s, rc = connect(p, '127.0.0.1', ECHO.port)
            ok = rc == 0
            for r in range(rounds):
                ok = ok and roundtrip(s, b'%d-%d' % (i, r))
            s.close()
            res.append(ok)
        except Exception:
            res.append(False)
    th = [threading.Thread(target=worker, args=(i,)) for i in range(nconn)]
    for t in th: t.start()
    for t in th: t.join()
    p.stop()
    return len(res) == nconn and all(res)

def load(p, n):
    """a mix of successful and failing sessions"""
    for i in range(n):
        k = i % 4
        if k == 0:
            s, rc = connect(p, '127.0.0.1', ECHO.port)
            roundtrip(s)
        elif k == 1:
            s, rc = connect(p, '127.0.0.1', 1)
        elif k == 2:
            s, _ = greet(p.port, b'\x07')
        else:
            s = socket.create_connection(('127.0.0.1', p.port))
        s.close()
    def burst():
        s, rc = connect(p, '127.0.0.1', ECHO.port)
        roundtrip(s)
        s.close()
    th = [threading.Thread(target=burst) for _ in range(n // 10)]
    for t in th: t.start()
    for t in th: t.join()

def settle(p):
    # finished threads are joined at the next accept()
    socket.create_connection(('127.0.0.1', p.port)).close()
    time.sleep(1 if not WRAP else 5)
    return p.stats()

def t_resources(b):
    """threads, fds and memory go back to the idle level after load and
    memory does not grow any more once the allocator has warmed up"""
    n = 400 if QUICK else 4000
    p = Proxy(b, '-q')
    idle = settle(p)
    rss = []
    for i in range(6):
        load(p, n)
        st = settle(p)
        rss.append(st['VmRSS'])
    p.stop()
    growth = rss[-1] - rss[2]
    INFO.append('    idle %s, after load %s; rss kB per round of %d sessions %s, growth after warm-up %+d kB'
                % (idle, st, n + n // 10, rss, growth))
    return st['Threads'] == idle['Threads'] and st['fds'] == idle['fds'] and growth <= 512

TESTS = [t_connect_ipv4, t_connect_dns, t_connect_ipv6, t_err_refused, t_err_unresolvable,
         t_err_bad_command, t_err_bad_atyp, t_err_bad_version, t_auth_required, t_auth_ok,
         t_auth_wrong, t_whitelist, t_auth_once, t_bind_addr, t_full_duplex_integrity,
         t_target_bulk_then_close, t_directions_independent, t_no_spin_on_reset,
         t_eof_from_client, t_eof_from_target, t_idle_kept, t_idle_timeout, t_concurrency,
         t_resources]
INFO = []

def supports_timeout(b):
    p = subprocess.Popen([b, '-h'], stderr=subprocess.PIPE)
    return b'-t timeout' in p.communicate()[1]

def main():
    global ECHO, ECHO6, DUAL, WHOAMI, BULK_SRV, FLOOD, NOREAD
    ECHO = Server(h_echo)
    DUAL = Server(h_echo, '::')
    BULK_SRV = Server(h_bulk)
    FLOOD = Server(h_flood)
    NOREAD = Server(h_noread)
    ECHO6 = Server(h_echo, '::1')
    WHOAMI = Server(h_whoami)
    failed = 0
    for b in sys.argv[1:]:
        print('== %s' % b)
        for t in TESTS:
            if os.environ.get('ONLY') and t.__name__[2:] not in os.environ['ONLY'].split(','):
                continue
            if t is t_idle_timeout and not supports_timeout(b):
                print('  %-28s SKIP (no -t option)' % t.__name__[2:])
                continue
            del INFO[:]
            t0 = time.time()
            try:
                ok = t(b)
            except Exception as e:
                ok = False
                INFO.append('    %r' % e)
            for p in PROXIES:
                p.stop()
            del PROXIES[:]
            failed += not ok
            print('  %-28s %s  %.1fs' % (t.__name__[2:], 'ok' if ok else 'FAIL', time.time() - t0))
            for l in INFO:
                print(l)
            sys.stdout.flush()
    print('\n%s' % ('ALL PASSED' if not failed else '%d FAILED' % failed))
    sys.exit(1 if failed else 0)

if __name__ == '__main__':
    main()
