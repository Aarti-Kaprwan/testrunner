import math

from tuqquery.tuq import QueryTests
from remote.remote_util import RemoteMachineShellConnection
from membase.api.rest_client import RestConnection

class QueriesOpsTests(QueryTests):
    def setUp(self):
        super(QueriesOpsTests, self).setUp()

    def suite_setUp(self):
        super(QueriesOpsTests, self).suite_setUp()

    def tearDown(self):
        super(QueriesOpsTests, self).tearDown()

    def suite_tearDown(self):
        super(QueriesOpsTests, self).suite_tearDown()

    def test_incr_rebalance_in(self):
        self.assertTrue(len(self.servers) >= self.nodes_in + 1, "Servers are not enough")
        self.test_order_by_over()
        for i in xrange(1, self.nodes_in + 1):
            rebalance = self.cluster.async_rebalance(self.servers[:i],
                                                     self.servers[i:i+1], [])
            self.test_order_by_over()
            rebalance.result()
            self.test_order_by_over()

    def test_incr_rebalance_out(self):
        self.assertTrue(len(self.servers[:self.nodes_init]) > self.nodes_out + 1,
                        "Servers are not enough")
        self.test_order_by_over()
        for i in xrange(1, self.nodes_out + 1):
            rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init - (i-1)],
                                    [],
                                    self.servers[self.nodes_init - i:self.nodes_init - (i-1)])
            self.test_order_by_over()
            rebalance.result()
            self.test_order_by_over()

    def test_swap_rebalance(self):
        self.assertTrue(len(self.servers) >= self.nodes_init + self.nodes_in,
                        "Servers are not enough")
        self.test_order_by_over()
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                               self.servers[self.nodes_init:self.nodes_init + self.nodes_in],
                               self.servers[self.nodes_init - self.nodes_out:self.nodes_init])
        self.test_order_by_over()
        rebalance.result()
        self.test_order_by_over()

    def test_rebalance_with_server_crash(self):
        servr_in = self.servers[self.nodes_init:self.nodes_init + self.nodes_in]
        servr_out = self.servers[self.nodes_init - self.nodes_out:self.nodes_init]
        self.test_group_by_over()
        for i in xrange(3):
            rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                     servr_in, servr_out)
            self.sleep(5, "Wait some time for rebalance process and then kill memcached")
            remote = RemoteMachineShellConnection(self.servers[self.nodes_init -1])
            remote.terminate_process(process_name='memcached')
            self.test_group_by_over()
            try:
                rebalance.result()
            except:
                pass
        self.cluster.rebalance(self.servers[:self.nodes_init], servr_in, servr_out)
        self.test_group_by_over()

    def test_failover(self):
        servr_out = self.servers[self.nodes_init - self.nodes_out:self.nodes_init]
        self.test_group_by_aggr_fn()
        self.cluster.failover(self.servers[:self.nodes_init], servr_out)
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                               [], servr_out)
        self.test_group_by_aggr_fn()
        rebalance.result()
        self.test_group_by_aggr_fn()

    def test_failover_add_back(self):
        servr_out = self.servers[self.nodes_init - self.nodes_out:self.nodes_init]
        self.test_group_by_aggr_fn()

        nodes_all = RestConnection(self.master).node_statuses()
        nodes = []
        for failover_node in servr_out:
            nodes.extend([node for node in nodes_all
                if node.ip != failover_node.ip or str(node.port) != failover_node.port])
        self.cluster.failover(self.servers[:self.nodes_init], servr_out)
        for node in nodes:
            RestConnection(self.master).add_back_node(node.id)
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init], [], [])
        self.test_group_by_aggr_fn()
        rebalance.result()
        self.test_group_by_aggr_fn()