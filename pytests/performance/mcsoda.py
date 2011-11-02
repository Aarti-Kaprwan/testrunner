#!/usr/bin/env python

import os
import sys
import math
import time
import socket
import string
import struct
import threading

sys.path.append("lib")

try:
   import logger
   log = logger.new_logger("mcsoda")
except:
   class P:
      def error(self, m): print(m)
      def info(self, m):  print(m)
   log = P()

try:
   from hashlib import md5
except ImportError:
   from md5 import md5

import crc32
import mc_bin_client
import memcacheConstants

from memcacheConstants import REQ_MAGIC_BYTE, RES_MAGIC_BYTE
from memcacheConstants import REQ_PKT_FMT, RES_PKT_FMT, MIN_RECV_PACKET
from memcacheConstants import SET_PKT_FMT, CMD_GET, CMD_SET, CMD_DELETE
from memcacheConstants import CMD_ADD, CMD_REPLACE, CMD_PREPEND, CMD_APPEND # "ARPA"

# --------------------------------------------------------

INT_TYPE = type(123)
FLOAT_TYPE = type(0.1)
DICT_TYPE = type({})

def dict_to_s(d, level="", res=[], suffix=", ", ljust=None):
   dtype = DICT_TYPE
   scalars = []
   complex = []
   for key in d.keys():
      if type(d[key]) == dtype:
         complex.append(key)
      else:
         scalars.append(key)
   scalars.sort()
   complex.sort()

   # Special case for histogram output.
   histo = 0
   if scalars and not complex and \
      type(scalars[0]) == FLOAT_TYPE and type(d[scalars[0]]) == INT_TYPE:
      for key in scalars:
         histo = max(d[key], histo)

   for key in scalars:
      k = str(key)
      if ljust:
         k = string.ljust(k, ljust)
      v = str(d[key])
      if histo:
         v = string.rjust(v, 8) + " " + ("*" * int(math.ceil(50.0 * d[key] / histo)))

      res.append(level + k + ": " + v + suffix)

   # Recurse for nested, dictionary values.
   if complex:
      res.append("\n")
   for key in complex:
      res.append(level + str(key) + ":\n")
      dict_to_s(d[key], level + "  ", res=res, suffix="\n", ljust=8)

   return ''.join(res)

# --------------------------------------------------------

MIN_VALUE_SIZE = [10]
REPORT_EVERY = 20000

def run_worker(ctl, cfg, cur, store, prefix):
    i = 0
    t_last = time.time()
    o_last = store.num_ops(cur)
    ops_per_sec_prev = []

    if cfg.get('max-ops-per-sec', 0) > 0 and not 'batch' in cur:
       cur['batch'] = 10

    while ctl.get('run_ok', True):
        num_ops = cur.get('cur-gets', 0) + cur.get('cur-sets', 0)

        if cfg.get('max-ops', 0) > 0 and cfg.get('max-ops', 0) <= num_ops:
            break
        if cfg.get('exit-after-creates', 0) > 0 and \
           cfg.get('max-creates', 0) > 0 and \
           cfg.get('max-creates', 0) <= cur.get('cur-creates', 0):
            break

        store.command(next_cmd(cfg, cur, store))
        i += 1

        if i % REPORT_EVERY == 0:
            t_curr = time.time()
            o_curr = store.num_ops(cur)

            t_delta = t_curr - t_last
            o_delta = o_curr - o_last

            ops_per_sec = o_delta / t_delta

            log.info(prefix + dict_to_s(cur))
            log.info("%s    ops: %s secs: %s ops/sec: %s" %
                     (prefix,
                      string.ljust(str(o_delta), 10),
                      string.ljust(str(t_delta), 15),
                      ops_per_sec))
            t_last = t_curr
            o_last = o_curr

            ops_per_sec_prev.append(ops_per_sec)
            while len(ops_per_sec_prev) > 10:
               ops_per_sec_prev.pop(0)

            max_ops_per_sec = cfg.get('max-ops-per-sec', 0)
            if max_ops_per_sec > 0 and len(ops_per_sec_prev) >= 2:
               # Do something clever here to prevent going over
               # the max-ops-per-sec.
               pass

    store.flush()

def next_cmd(cfg, cur, store):
    num_ops = cur.get('cur-gets', 0) + cur.get('cur-sets', 0)

    do_set = cfg.get('ratio-sets', 0) > float(cur.get('cur-sets', 0)) / positive(num_ops)
    if do_set:
        cmd = 'set'
        cur['cur-sets'] = cur.get('cur-sets', 0) + 1

        do_set_create = (cfg.get('max-items', 0) > cur.get('cur-items', 0) and
                         cfg.get('max-creates', 0) > cur.get('cur-creates', 0) and
                         cfg.get('ratio-creates', 0) > \
                           float(cur.get('cur-creates', 0)) / positive(cur.get('cur-sets', 0)))
        if do_set_create:
            # Create...
            key_num = cur.get('cur-items', 0)

            cur['cur-items'] = cur.get('cur-items', 0) + 1
            cur['cur-creates'] = cur.get('cur-creates', 0) + 1
        else:
            # Update...
            num_updates = cur['cur-sets'] - cur.get('cur-creates', 0)

            do_delete = cfg.get('ratio-deletes', 0) > \
                          float(cur.get('cur-deletes', 0)) / positive(num_updates)
            if do_delete:
               cmd = 'delete'
               cur['cur-deletes'] = cur.get('cur-deletes', 0) + 1
            else:
               num_mutates = num_updates - cur.get('cur-deletes', 0)

               do_arpa = cfg.get('ratio-arpas', 0) > \
                           float(cur.get('cur-arpas', 0)) / positive(num_mutates)
               if do_arpa:
                  cmd = 'arpa'
                  cur['cur-arpas'] = cur.get('cur-arpas', 0) + 1

            key_num = choose_key_num(cur.get('cur-items', 0),
                                     cfg.get('ratio-hot', 0),
                                     cfg.get('ratio-hot-sets', 0),
                                     cur.get('cur-sets', 0))

        key_str = prepare_key(key_num, cfg.get('prefix', ''))
        itm_val = store.gen_doc(key_num, key_str,
                                choose_entry(cfg.get('min-value-size', MIN_VALUE_SIZE),
                                             num_ops))

        return (cmd, key_num, key_str, itm_val)
    else:
        cmd = 'get'
        cur['cur-gets'] = cur.get('cur-gets', 0) + 1

        do_get_hit = (cfg.get('ratio-misses', 0) * 100) < (cur.get('cur-gets', 0) % 100)
        if do_get_hit:
            key_num = choose_key_num(cur.get('cur-items', 0),
                                     cfg.get('ratio-hot', 0),
                                     cfg.get('ratio-hot-gets', 0),
                                     cur.get('cur-gets', 0))
            key_str = prepare_key(key_num, cfg.get('prefix', ''))
            itm_val = store.gen_doc(key_num, key_str,
                                    choose_entry(cfg.get('min-value-size', MIN_VALUE_SIZE),
                                                 num_ops))

            return (cmd, key_num, key_str, itm_val)
        else:
            cur['cur-misses'] = cur.get('cur-misses', 0) + 1
            return (cmd, -1, prepare_key(-1, cfg.get('prefix', '')), None)

def choose_key_num(num_items, ratio_hot, ratio_hot_choice, num_ops):
    hit_hot_range = (ratio_hot_choice * 100) > (num_ops % 100)
    if hit_hot_range:
        base  = 0
        range = math.floor(ratio_hot * num_items)
    else:
        base  = math.floor(ratio_hot * num_items)
        range = math.floor((1.0 - ratio_hot) * num_items)

    return int(base + (num_ops % positive(range)))

def positive(x):
    if x > 0:
        return x
    return 1

def prepare_key(key_num, prefix=None):
    key_hash = md5(str(key_num)).hexdigest()[0:16]
    if prefix and len(prefix) > 0:
        return prefix + "-" + key_hash
    return key_hash

def choose_entry(arr, n):
    return arr[n % len(arr)]

# --------------------------------------------------------

class Store:

    def connect(self, target, user, pswd, cfg, cur, vbucket_count):
        self.cfg = cfg
        self.cur = cur

    def stats_collector(self, sc):
        self.sc = sc

    def command(self, c):
        log.info("%s %s %s %s" % c)

    def flush(self):
        pass

    def num_ops(self, cur):
        return cur.get('cur-gets', 0) + cur.get('cur-sets', 0)

    def gen_doc(self, key_num, key_str, min_value_size):
        return gen_doc_string(key_num, key_str, min_value_size,
                              self.cfg['suffix'][min_value_size],
                              self.cfg.get('json', 1) > 0)

    def cmd_line_get(self, key_num, key_str):
        return key_str

    def readbytes(self, skt, nbytes, buf):
        while len(buf) < nbytes:
            data = skt.recv(max(nbytes - len(buf), 4096))
            if not data:
                return None, ''
            buf += data
        return buf[:nbytes], buf[nbytes:]

    def add_timing_sample(self, cmd, delta, prefix="latency-"):
       histo = self.cur.get(prefix + cmd, None)
       if histo is None:
          histo = {}
          self.cur[prefix + cmd] = histo
       bucket = 10 ** math.floor(math.log10(delta))
       histo[bucket] = histo.get(bucket, 0) + 1


class StoreMemcachedBinary(Store):

    def connect(self, target, user, pswd, cfg, cur, vbucket_count):
        self.cfg = cfg
        self.cur = cur
        self.target = target
        self.host_port = (target + ":11211").split(':')[0:2]
        self.host_port[1] = int(self.host_port[1])
        self.conn = mc_bin_client.MemcachedClient(self.host_port[0],
                                                  self.host_port[1])
        self.vbucket_count = vbucket_count
        if user:
           self.conn.sasl_auth_plain(user, pswd)
        self.inflight_reinit()
        self.queue = []
        self.ops = 0
        self.buf = ''
        self.arpa = [ (CMD_ADD,     True),
                      (CMD_REPLACE, True),
                      (CMD_APPEND,  False),
                      (CMD_PREPEND, False) ]

    def inflight_reinit(self, inflight=0):
        self.inflight = inflight
        self.inflight_num_gets = 0
        self.inflight_num_sets = 0
        self.inflight_num_deletes = 0
        self.inflight_num_arpas = 0
        self.inflight_start_time = 0
        self.inflight_end_time = 0

    def command(self, c):
        self.queue.append(c)
        if len(self.queue) > (self.cur.get('batch') or \
                              self.cfg.get('batch', 100)):
            self.flush()

    def header(self, op, key, val, opaque=0, extra='', cas=0,
               dtype=0, vbucketId=0,
               fmt=REQ_PKT_FMT,
               magic=REQ_MAGIC_BYTE):
        vbucketId = crc32.crc32_hash(key) & (self.vbucket_count - 1)
        return struct.pack(fmt, magic, op,
                           len(key), len(extra), dtype, vbucketId,
                           len(key) + len(extra) + len(val), opaque, cas)

    def flush(self):
        extra = struct.pack(SET_PKT_FMT, 0, self.cfg.get('expiration', 0))

        if self.inflight > 0:
           for i in range(self.inflight):
              self.recvMsg()
           self.inflight_end_time = time.time()
           self.ops += self.inflight
           if self.sc:
              self.sc.ops_stats({ 'tot-gets':    self.inflight_num_gets,
                                  'tot-sets':    self.inflight_num_sets,
                                  'tot-deletes': self.inflight_num_deletes,
                                  'tot-arpas':   self.inflight_num_arpas,
                                  'start-time':  self.inflight_start_time,
                                  'end-time':    self.inflight_end_time })
           self.inflight_reinit()

        if len(self.queue) > 0:
           # Use the first request to measure single request latency.
           #
           m = []
           cmd, key_num, key_str, data = self.queue.pop(0)
           delta_gets, delta_sets, delta_deletes, delta_arpas = \
                self.cmd_append(cmd, key_num, key_str, data, m, extra)
           msg = ''.join(m)

           start = time.time()
           self.conn.s.send(msg)
           self.recvMsg()
           end = time.time()

           if self.sc:
              self.sc.latency_stats({ 'tot-gets': delta_gets,
                                      'tot-sets': delta_sets,
                                      'tot-deletes': delta_deletes,
                                      'tot-arpas': delta_arpas,
                                      'start-time': start,
                                      'end-time': end })

           self.add_timing_sample(cmd, end - start)

        if not self.queue:
           return

        m = []
        for c in self.queue:
            cmd, key_num, key_str, data = c
            delta_gets, delta_sets, delta_deletes, delta_arpas = \
                self.cmd_append(cmd, key_num, key_str, data, m, extra)
            self.inflight_num_gets += delta_gets
            self.inflight_num_sets += delta_sets
            self.inflight_num_deletes += delta_deletes
            self.inflight_num_arpas += delta_arpas

        self.inflight_reinit(len(self.queue))
        self.queue = []
        msg = ''.join(m)

        self.inflight_start_time = time.time()
        self.conn.s.send(msg)

    def cmd_append(self, cmd, key_num, key_str, data, m, extra):
       if cmd[0] == 'g':
          m.append(self.header(CMD_GET, key_str, ''))
          m.append(key_str)
          return 1, 0, 0, 0
       elif cmd[0] == 'd':
          m.append(self.header(CMD_DELETE, key_str, ''))
          m.append(key_str)
          return 0, 0, 1, 0

       rv = (0, 1, 0, 0)
       curr_cmd = CMD_SET
       curr_extra = extra

       if cmd[0] == 'a':
          rv = (0, 0, 0, 1)
          curr_cmd, have_extra = self.arpa[self.cur.get('cur-sets', 0) % len(self.arpa)]
          if not have_extra:
             curr_extra = ''

       m.append(self.header(curr_cmd, key_str, data, extra=curr_extra))
       if curr_extra:
          m.append(extra)
       m.append(key_str)
       m.append(data)
       return rv

    def num_ops(self, cur):
        return self.ops

    def recvMsg(self):
        buf = self.buf
        pkt, buf = self.readbytes(self.conn.s, MIN_RECV_PACKET, buf)
        magic, cmd, keylen, extralen, dtype, errcode, datalen, opaque, cas = \
            struct.unpack(RES_PKT_FMT, pkt)
        val, buf = self.readbytes(self.conn.s, datalen, buf)
        self.buf = buf


class StoreMemcachedAscii(Store):

    def connect(self, target, user, pswd, cfg, cur, vbucket_count):
        self.cfg = cfg
        self.cur = cur
        self.target = target
        self.host_port = (target + ":11211").split(':')[0:2]
        self.host_port[1] = int(self.host_port[1])
        self.skt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.skt.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.skt.connect(tuple(self.host_port))
        self.queue = []
        self.ops = 0
        self.buf = ''
        self.arpa = [ 'add', 'replace', 'append', 'prepend' ]

    def command(self, c):
        self.queue.append(c)
        if len(self.queue) > (self.cur.get('batch') or \
                              self.cfg.get('batch', 100)):
            self.flush()

    def command_send(self, cmd, key_num, key_str, data):
        if cmd[0] == 'g':
            return 'get ' + key_str + '\r\n'
        if cmd[0] == 'd':
            return 'delete ' + key_str + '\r\n'

        c = 'set'
        if cmd[0] == 'a':
           c = self.arpa[self.cur.get('cur-sets', 0) % len(self.arpa)]
        return "%s %s 0 %s %s\r\n%s\r\n" % (c, key_str, self.cfg.get('expiration', 0),
                                            len(data), data)

    def command_recv(self, cmd, key_num, key_str, data):
        buf = self.buf
        if cmd[0] == 'g':
            # GET...
            line, buf = self.readline(self.skt, buf)
            while line and line != 'END':
                # line == "VALUE k flags len"
                rvalue, rkey, rflags, rlen = line.split()
                data, buf = self.readbytes(self.skt, int(rlen) + 2, buf)
                line, buf = self.readline(self.skt, buf)
        elif cmd[0] == 'd':
            # DELETE...
            line, buf = self.readline(self.skt, buf) # line == "DELETED"
        else:
            # SET...
            line, buf = self.readline(self.skt, buf) # line == "STORED"
        self.buf = buf

    def flush(self):
        m = []
        for c in self.queue:
            cmd, key_num, key_str, data = c
            m.append(self.command_send(cmd, key_num, key_str, data))

        self.skt.send(''.join(m))

        for c in self.queue:
            cmd, key_num, key_str, data = c
            self.command_recv(cmd, key_num, key_str, data)

        self.ops += len(self.queue)
        self.queue = []

    def num_ops(self, cur):
        return self.ops

    def readline(self, skt, buf):
        while True:
            index = buf.find('\r\n')
            if index >= 0:
                break
            data = skt.recv(4096)
            if not data:
                return '', ''
            buf += data
        return buf[:index], buf[index+2:]

# --------------------------------------------------------

# A key is a 16 char hex string.
def key_to_name(key_num, key_str):
   return key_str[0:4] + " " + key_str[-4:-1]
def key_to_email(key_num, key_str):
   return key_str[0:4] + "@" + key_str[3:5] + ".com"
def key_to_city(key_num, key_str):
   return key_str[4:7]
def key_to_country(key_num, key_str):
   return key_str[7:9]
def key_to_realm(key_num, key_str):
   return key_str[9:12]

def gen_doc_string(key_num, key_str, min_value_size, suffix, json,
                   key_name="key"):
    c = "{"
    if not json:
        c = "*"
    s = """%s"%s":"%s",
 "key_num":%s,
 "name":"%s",
 "email":"%s",
 "city":"%s",
 "country":"%s",
 "realm":"%s",
 "coins":%s,
 "achievements":%s,
 %s"""

    next = 300
    achievements = []
    for i in range(len(key_str)):
       next = (next + int(key_str[i], 16) * i) % 500
       if next < 256:
          achievements.append(next)

    return s % (c, key_name, key_str,
                key_num,
                key_to_name(key_num, key_str),
                key_to_email(key_num, key_str),
                key_to_city(key_num, key_str),
                key_to_country(key_num, key_str),
                key_to_realm(key_num, key_str),
                max(0.0, int(key_str[0:4], 16) / 100.0), # coins
                achievements,
                suffix)

# --------------------------------------------------------

def run(cfg, cur, protocol, host_port, user, pswd,
        stats_collector = None, vbucket_count=1024):
   if type(cfg['min-value-size']) == type(""):
       cfg['min-value-size'] = string.split(cfg['min-value-size'], ",")
   if type(cfg['min-value-size']) != type([]):
       cfg['min-value-size'] = [ cfg['min-value-size'] ]

   cfg['body'] = {}
   cfg['suffix'] = {}

   for i in range(len(cfg['min-value-size'])):
       mvs = int(cfg['min-value-size'][i])
       cfg['min-value-size'][i] = mvs
       cfg['body'][mvs] = 'x'
       while len(cfg['body'][mvs]) < mvs:
          cfg['body'][mvs] = cfg['body'][mvs] + \
                             md5(str(len(cfg['body'][mvs]))).hexdigest()
       cfg['suffix'][mvs] = "\"body\":\"" + cfg['body'][mvs] + "\"}"

   ctl = { 'run_ok': True }

   threads = []

   for i in range(cfg.get('threads', 1)):
      store = Store()
      if protocol.split('-')[0].find('memcache') >= 0:
         if protocol.split('-')[1] == 'ascii':
            store = StoreMemcachedAscii()
         else:
            store = StoreMemcachedBinary()

      store.connect(host_port, user, pswd, cfg, cur, vbucket_count)
      store.stats_collector(stats_collector)

      threads.append(threading.Thread(target=run_worker,
                                      args=(ctl, cfg, cur, store,
                                            "thread-" + str(i) + ": ")))

   log.info("first 5 keys...")
   for i in range(5):
      print("echo get %s | nc %s %s" %
            (store.cmd_line_get(i, prepare_key(i, cfg.get('prefix', ''))),
             host_port.split(':')[0],
             host_port.split(':')[1]))

   def stop_after(secs):
      time.sleep(secs)
      ctl['run_ok'] = False

   if cfg.get('time', 0) > 0:
      t = threading.Thread(target=stop_after, args=(cfg.get('time', 0),))
      t.daemon = True
      t.start()

   t_start = time.time()

   try:
      if len(threads) <= 1:
         run_worker(ctl, cfg, cur, store, "")
      else:
         for thread in threads:
            thread.daemon = True
            thread.start()

         while len(threads) > 0:
            threads[0].join(1)
            threads = [t for t in threads if t.isAlive()]
   except KeyboardInterrupt:
      ctl['run_ok'] = False

   t_end = time.time()

   log.info("")
   log.info(dict_to_s(cur))
   log.info("    ops/sec: %s" %
            ((cur.get('cur-gets', 0) + cur.get('cur-sets', 0)) / (t_end - t_start)))

   threads = [t for t in threads if t.isAlive()]
   while len(threads) > 0:
      threads[0].join(1)
      threads = [t for t in threads if t.isAlive()]

   return cur, t_start, t_end


if __name__ == "__main__":
  cfg_defaults = {
     "prefix":             ("",   "Prefix for every item key."),
     "max-ops":            (0,    "Max number of ops before exiting. 0 means keep going."),
     "max-items":          (-1,   "Max number of items; default 100000."),
     "max-creates":        (-1,   "Max number of creates; defaults to max-items."),
     "min-value-size":     ("10", "Minimal value size (bytes) during SET's; comma-separated."),
     "ratio-sets":         (0.1,  "Fraction of requests that should be SET's."),
     "ratio-creates":      (0.1,  "Fraction of SET's that should create new items."),
     "ratio-misses":       (0.05, "Fraction of GET's that should miss."),
     "ratio-hot":          (0.2,  "Fraction of items to have as a hot item subset."),
     "ratio-hot-sets":     (0.95, "Fraction of SET's that hit the hot item subset."),
     "ratio-hot-gets":     (0.95, "Fraction of GET's that hit the hot item subset."),
     "ratio-deletes":      (0.0,  "Fraction of SET updates that should be DELETE's instead."),
     "ratio-arpas":        (0.0,  "Fraction of SET non-DELETE'S to be 'a-r-p-a' cmds."),
     "expiration":         (0,    "Expiration time parameter for SET's"),
     "exit-after-creates": (0,    "Exit after max-creates is reached."),
     "threads":            (1,    "Number of client worker threads to use."),
     "batch":              (100,  "Batch / pipeline up this number of commands."),
     "json":               (1,    "Use JSON documents. 0 to generate binary documents."),
     "time":               (0,    "Stop after this many seconds if > 0."),
     "max-ops-per-sec":    (0,    "Max ops/second, which overrides the batch parameter.")
     }

  cur_defaults = {
     "cur-items":    (0, "Number of items known to already exist."),
     "cur-sets":     (0, "Number of sets already done."),
     "cur-creates":  (0, "Number of sets that were creates."),
     "cur-gets":     (0, "Number of gets already done."),
     "cur-deletes":  (0, "Number of deletes already done."),
     "cur-arpas":    (0, "Number of add/replace/prepend/append's (a-r-p-a) commands."),
     }

  if len(sys.argv) < 2 or "-h" in sys.argv or "--help" in sys.argv:
     print("usage: %s [memcached[-binary|-ascii]://][user[:pswd]@]host[:port] [key=val]*\n" %
           (sys.argv[0]))
     print("  default protocol = memcached-binary://")
     print("  default port     = 11211\n")
     for s in ["examples: %s memcached-binary://127.0.0.1:11211 max-items=1000000 json=1",
               "          %s memcached://127.0.0.1:11211",
               "          %s 127.0.0.1:11211",
               "          %s 127.0.0.1",
               "          %s my-test-bucket@127.0.0.1",
               "          %s my-test-bucket:MyPassword@127.0.0.1"]:
        print(s % (sys.argv[0]))
     print("")
     print("optional key=val's and their defaults:")
     for d in [cfg_defaults, cur_defaults]:
        for k in sorted(d.iterkeys()):
           print("  %s = %s %s" %
                 (string.ljust(k, 20), string.ljust(str(d[k][0]), 4), d[k][1]))
     print("")
     print("  TIP: min-value-size can be comma-separated values: min-value-size=10,256,1024")
     print("")
     sys.exit(-1)

  cfg = {}
  cur = {}
  err = {}

  for (o, d) in [(cfg, cfg_defaults), (cur, cur_defaults)]: # Parse key=val pairs.
     for (dk, dv) in d.iteritems():
        o[dk] = dv[0]
     for kv in sys.argv[2:]:
        k, v = (kv + '=').split('=')[0:2]
        if k and v and k in o:
           if type(o[k]) != type(""):
              try:
                 v = ({ 'y':'1', 'n':'0' }).get(v, v)
                 for parse in [float, int]:
                    if str(parse(v)) == v:
                       v = parse(v)
              except:
                 err[kv] = err.get(kv, 0) + 1
           o[k] = v
        else:
           err[kv] = err.get(kv, 0) + 1

  for kv in err:
     if err[kv] > 1:
        log.error("problem parsing key=val option: " + kv)
  for kv in err:
     if err[kv] > 1:
        sys.exit(-1)

  if cfg.get('max-items', 0) < 0 and cfg.get('max-creates', 0) < 0:
     cfg['max-items'] = 100000
  if cfg.get('max-items', 0) < 0:
     cfg['max-items'] = cfg.get('max-creates', 0)
  if cfg.get('max-creates', 0) < 0:
     cfg['max-creates'] = cfg.get('max-items', 0)

  for o in [cfg, cur]:
     for k in sorted(o.iterkeys()):
        log.info("    %s = %s" % (string.ljust(k, 20), o[k]))

  protocol = (["memcached"] + sys.argv[1].split("://"))[-2] + "-binary"
  host_port = ('@' + sys.argv[1].split("://")[-1]).split('@')[-1] + ":11211"
  user, pswd = (('@' + sys.argv[1].split("://")[-1]).split('@')[-2] + ":").split(':')[0:2]

  run(cfg, cur, protocol, host_port, user, pswd)
