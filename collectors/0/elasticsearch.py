#!/usr/bin/env python
# This file is part of tcollector.
# Copyright (C) 2011-2013  The tcollector Authors.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser
# General Public License for more details.  You should have received a copy
# of the GNU Lesser General Public License along with this program.  If not,
# see <http://www.gnu.org/licenses/>.

"""ElasticSearch collector"""  # Because ES is cool, bonsai cool.
# Tested with ES 0.16.5, 0.17.x, 0.90.1 .
import base64
import re
import socket
import sys
import threading
import time

try:
    from urllib2 import urlopen, Request
    from urllib2 import HTTPError, URLError
except ImportError:
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError

try:
    import json
except ImportError:
    json = None  # Handled gracefully in main.  Not available by default in <2.6

from collectors.lib import utils
from collectors.etc import elasticsearch_conf

COLLECTION_INTERVAL = 60  # seconds
DEFAULT_TIMEOUT = 10.0  # seconds

# regexes to separate differences in version numbers
PRE_VER1 = re.compile(r'^0\.')
VER1 = re.compile(r'^1\.')

STATUS_MAP = {
    "green": 0,
    "yellow": 1,
    "red": 2,
}

rootmetric = "elasticsearch"

REGISTERED_METRIC_TAGS = {
    rootmetric+".node.ingest.pipelines":"pipeline",
    rootmetric+".node.adaptive_selection":"asid",
    rootmetric+".node.thread_pool":"threadpool"
}


class ESError(RuntimeError):
    """Exception raised if we don't get a 200 OK from ElasticSearch."""

    def __init__(self, resp):
        RuntimeError.__init__(self, str(resp))
        self.resp = resp


def build_http_url(host, port, uri):
    if port == 443:
        protocol = "https"
    else:
        protocol = "http"
    return "%s://%s:%s%s" % (protocol, host, port, uri)


def request(server, uri, json_in=True):
    url = build_http_url(server["host"], server["port"], uri)
    headers = server["headers"]
    req = Request(url)
    for key in headers.keys():
        req.add_header(key, headers[key])

    try:
        resp = urlopen(req, timeout=DEFAULT_TIMEOUT)
        resp_body = resp.read()
        if json_in:
            return json.loads(resp_body)
        else:
            return resp_body
    except HTTPError as e:
        utils.err(e)
    except URLError as e:
        utils.err(e)
        utils.err('We failed to reach a server.')


def cluster_health(server):
    return request(server, "/_cluster/health")


def cluster_stats(server):
    return request(server, "/_cluster/stats")


def cluster_master_node(server):
    return request(server, "/_cat/master", json_in=False).split()[0]


def index_stats(server):
    return request(server, "/_all/_stats")


def node_status(server):
    return request(server, "/")


def node_stats(server, version):
    # API changed in v1.0
    if PRE_VER1.match(version):
        url = "/_cluster/nodes/_local/stats"
    else:
        url = "/_nodes/stats"
    return request(server, url)


def printmetric(metric, ts, value, tags):
    # Warning, this should be called inside a lock
    if tags:
        tags = " " + " ".join("%s=%s" % (name.replace(" ", ""), value.replace(" ", "").replace(":", "-"))
                              for name, value in tags.items())
    else:
        tags = ""
    print("%s %d %s %s"
          % (metric, ts, value, tags))


def _traverse(metric, stats, ts, tags, check=True):
    """
       Recursively traverse the json tree and print out leaf numeric values
       Please make sure you call this inside a lock and don't add locking
       inside this function
    """
    # print metric,stats,ts,tags
    if isinstance(stats, dict):
        if "timestamp" in stats:
            ts = stats["timestamp"] / 1000  # ms -> s
        for key in list(stats.keys()):
            if key != "timestamp":
                if metric in REGISTERED_METRIC_TAGS:
                    if check:
                        registeredMetricTags = tags.copy()
                        registeredMetricTags[REGISTERED_METRIC_TAGS.get(metric)] = key
                        _traverse(metric, stats[key], ts, registeredMetricTags, False)
                    else:
                        _traverse(metric + "." + key, stats[key], ts, tags)
                else:
                    _traverse(metric + "." + key, stats[key], ts, tags)
    if isinstance(stats, (list, set, tuple)):
        count = 0
        for value in stats:
            _traverse(metric + "." + str(count), value, ts, tags)
            count += 1
    if utils.is_numeric(stats) and not isinstance(stats, bool):
        if isinstance(stats, int):
            stats = int(stats)
        printmetric(metric, ts, stats, tags)
    return


def _collect_indices_total(metric, stats, tags, lock):
    ts = int(time.time())
    with lock:
        _traverse(metric, stats, ts, tags)


def _collect_indices_stats(metric, indexStats, tags, lock):
    ts = int(time.time())
    with lock:
        _traverse(metric, indexStats, ts, tags)


def _collect_indices(server, metric, tags, lock):
    indexStats = index_stats(server)
    totalStats = indexStats["_all"]
    _collect_indices_total(metric + ".indices", totalStats, tags, lock)

    indicesStats = indexStats["indices"]
    while len(indicesStats) != 0:
        indexId, stats = indicesStats.popitem()
        indextags = {"cluster": tags["cluster"], "index": indexId}
        _collect_indices_stats(metric + ".indices.byindex", stats, indextags, lock)


def _collect_master(server, metric, tags, lock):
    ts = int(time.time())
    chealth = cluster_health(server)
    if "status" in chealth:
        with lock:
            printmetric(metric + ".cluster.status", ts,
                        STATUS_MAP.get(chealth["status"], -1), tags)
    with lock:
        _traverse(metric + ".cluster", chealth, ts, tags)

    ts = int(time.time())  # In case last call took a while.
    cstats = cluster_stats(server)
    with lock:
        _traverse(metric + ".cluster", cstats, ts, tags)


def _collect_server(server, version, lock):
    ts = int(time.time())
    nstats = node_stats(server, version)
    cluster_name = nstats["cluster_name"]
    _collect_cluster_stats(cluster_name, lock, rootmetric, server)
    while len(nstats["nodes"]) != 0:
        nodeid, nodeStats = nstats["nodes"].popitem()
        node_name = nodeStats["name"]
        tags = {"cluster": cluster_name, "node": node_name, "nodeid": nodeid}
        with lock:
            _traverse(rootmetric+".node", nodeStats, ts, tags)


def _collect_cluster_stats(cluster_name, lock, rootmetric, server):
    clusterTags = {"cluster": cluster_name}
    _collect_master(server, rootmetric, clusterTags, lock)
    _collect_indices(server, rootmetric, clusterTags, lock)


def get_live_servers():
    servers = []
    for conf in elasticsearch_conf.get_servers():
        host = conf[0]
        port = conf[1]
        if (len(conf) == 4):
            user = conf[2]
            password = conf[3]
            headers = {'Authorization': 'Basic %s' %
                                        base64.b64encode(user + ":" + password)}
        else:
            headers = {}

        server = {"host": host, "port": port, "headers": headers}
        try:
            status = node_status(server)
            if status:
                servers.append(server)
        except Exception as err:
            utils.err(err)
            utils.err("Error getting node status")
            continue
    return servers


def main(argv):
    utils.drop_privileges()
    socket.setdefaulttimeout(DEFAULT_TIMEOUT)

    if json is None:
        utils.err("This collector requires the `json' Python module.")
        return 1

    servers = get_live_servers()

    if len(servers) == 0:
        return 13  # No ES running, ask tcollector to not respawn us.

    lock = threading.Lock()
    while True:
        threads = []
        t0 = int(time.time())
        utils.err("Fetching elasticsearch metrics")
        for server in servers:
            try:
                status = node_status(server)
            except Exception as err:
                utils.err(err)
                utils.err("Error getting node status")
                continue
            version = status["version"]["number"]
            t = threading.Thread(target=_collect_server, args=(server, version, lock))
            t.start()
            threads.append(t)
        for thread in threads:
            thread.join(DEFAULT_TIMEOUT)

        utils.err("Done fetching elasticsearch metrics in [%d]s " % (int(time.time()) - t0))
        time.sleep(COLLECTION_INTERVAL)


if __name__ == "__main__":
    sys.exit(main(sys.argv))