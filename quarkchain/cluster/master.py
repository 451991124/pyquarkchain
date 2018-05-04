import argparse
import asyncio
import ipaddress
import json
import random
from collections import deque

from quarkchain.config import DEFAULT_ENV
from quarkchain.cluster.rpc import ConnectToSlavesRequest, ClusterOp, CLUSTER_OP_SERIALIZER_MAP, Ping, SlaveInfo
from quarkchain.cluster.rpc import (
    AddMinorBlockHeaderResponse, GetEcoInfoListRequest,
    GetNextBlockToMineRequest, GetUnconfirmedHeadersRequest,
    GetTransactionCountRequest, AddTransactionRequest,
)
from quarkchain.cluster.protocol import ClusterMetadata, ClusterConnection, ROOT_BRANCH
from quarkchain.core import Branch, ShardMask
from quarkchain.db import PersistentDb
from quarkchain.cluster.jsonrpc import JSONRPCServer
from quarkchain.cluster.root_state import RootState
from quarkchain.cluster.simple_network import SimpleNetwork
from quarkchain.utils import set_logging_level, Logger, check


class ClusterConfig:

    def __init__(self, config):
        self.config = config

    def getSlaveInfoList(self):
        results = []
        for slave in self.config["slaves"]:
            ip = int(ipaddress.ip_address(slave["ip"]))
            results.append(SlaveInfo(slave["id"], ip, slave["port"], [ShardMask(v) for v in slave["shard_masks"]]))
        return results


class SlaveConnection(ClusterConnection):
    OP_NONRPC_MAP = {}

    def __init__(self, env, reader, writer, masterServer, slaveId, shardMaskList):
        super().__init__(env, reader, writer, CLUSTER_OP_SERIALIZER_MAP, self.OP_NONRPC_MAP, OP_RPC_MAP)
        self.masterServer = masterServer
        self.id = slaveId
        self.shardMaskList = shardMaskList
        check(len(shardMaskList) > 0)

        asyncio.ensure_future(self.activeAndLoopForever())

    def hasShard(self, shardId):
        for shardMask in self.shardMaskList:
            if shardMask.containShardId(shardId):
                return True
        return False

    async def sendPing(self):
        req = Ping("", [])
        op, resp, rpcId = await self.writeRpcRequest(
            op=ClusterOp.PING,
            cmd=req,
            metadata=ClusterMetadata(branch=ROOT_BRANCH, peerId=bytes(32)))
        return (resp.id, resp.shardMaskList)

    async def sendConnectToSlaves(self, slaveInfoList):
        ''' Make slave connect to other slaves.
        Returns True on success
        '''
        req = ConnectToSlavesRequest(slaveInfoList)
        op, resp, rpcId = await self.writeRpcRequest(ClusterOp.CONNECT_TO_SLAVES_REQUEST, req)
        check(len(resp.resultList) == len(slaveInfoList))
        for i, result in enumerate(resp.resultList):
            if len(result) > 0:
                Logger.info("Slave {} failed to connect to {} with error {}".format(
                    self.id, slaveInfoList[i].id, result))
                return False
        Logger.info("Slave {} connected to other slaves successfully".format(self.id))
        return True

    def close(self):
        Logger.info("Lost connection with slave {}".format(self.id))
        super().close()
        self.masterServer.shutdown()

    def closeWithError(self, error):
        Logger.info("Closing connection with slave {}".format(self.id))
        return super().closeWithError(error)

    async def getTransactionCount(self, address):
        request = GetTransactionCountRequest(address)
        _, resp, _ = await self.writeRpcRequest(
            ClusterOp.GET_TRANSACTION_COUNT_REQUEST,
            request,
        )
        check(resp.errorCode == 0)
        return resp.count

    async def addTransaction(self, tx):
        request = AddTransactionRequest(tx)
        _, resp, _ = await self.writeRpcRequest(
            ClusterOp.ADD_TRANSACTION_REQUEST,
            request,
        )
        return resp.errorCode == 0

    # RPC handlers

    async def handleAddMinorBlockHeaderRequest(self, req):
        self.masterServer.rootState.addValidatedMinorBlockHash(req.minorBlockHeader.getHash())
        return AddMinorBlockHeaderResponse(
            errorCode=0,
        )


OP_RPC_MAP = {
    ClusterOp.ADD_MINOR_BLOCK_HEADER_REQUEST: (
        ClusterOp.ADD_MINOR_BLOCK_HEADER_RESPONSE, SlaveConnection.handleAddMinorBlockHeaderRequest),
}


class MasterServer():
    ''' Master node in a cluster
    It does two things to initialize the cluster:
    1. Setup connection with all the slaves in ClusterConfig
    2. Make slaves connect to each other
    '''

    def __init__(self, env, rootState):
        self.loop = asyncio.get_event_loop()
        self.env = env
        self.rootState = rootState
        self.clusterConfig = env.clusterConfig.CONFIG

        # shard id -> a list of slave running the shard
        self.shardToSlaves = [[] for i in range(self.__getShardSize())]
        self.slavePool = set()

        self.clusterActiveFuture = self.loop.create_future()
        self.shutdownFuture = self.loop.create_future()
        self.rootBlockUpdateQueue = deque()
        self.isUpdatingRootBlock = False

    def __getShardSize(self):
        # TODO: replace it with dynamic size
        return self.env.config.SHARD_SIZE

    def __hasAllShards(self):
        ''' Returns True if all the shards have been run by at least one node '''
        return all([len(slaves) > 0 for slaves in self.shardToSlaves])

    async def __connect(self, ip, port):
        ''' Retries until success '''
        Logger.info("Trying to connect {}:{}".format(ip, port))
        while True:
            try:
                reader, writer = await asyncio.open_connection(ip, port, loop=self.loop)
                break
            except Exception as e:
                Logger.info("Failed to connect {} {}: {}".format(ip, port, e))
                await asyncio.sleep(self.env.clusterConfig.MASTER_TO_SLAVE_CONNECT_RETRY_DELAY)
        Logger.info("Connected to {}:{}".format(ip, port))
        return (reader, writer)

    async def __connectToSlaves(self):
        ''' Master connects to all the slaves '''
        for slaveInfo in self.clusterConfig.getSlaveInfoList():
            ip = str(ipaddress.ip_address(slaveInfo.ip))
            reader, writer = await self.__connect(ip, slaveInfo.port)

            slave = SlaveConnection(self.env, reader, writer, self, slaveInfo.id, slaveInfo.shardMaskList)
            await slave.waitUntilActive()

            # Verify the slave does have the same id and shard mask list as the config file
            id, shardMaskList = await slave.sendPing()
            if id != slaveInfo.id:
                Logger.error("Slave id does not match. expect {} got {}".format(slaveInfo.id, id))
                self.shutdown()
            if shardMaskList != slaveInfo.shardMaskList:
                Logger.error("Slave {} shard mask list does not match. expect {} got {}".format(
                    slaveInfo.id, slaveInfo.shardMaskList, shardMaskList))
                self.shutdown()

            self.slavePool.add(slave)
            for shardId in range(self.__getShardSize()):
                if slave.hasShard(shardId):
                    self.shardToSlaves[shardId].append(slave)

    async def __setupSlaveToSlaveConnections(self):
        ''' Make slaves connect to other slaves.
        Retries until success.
        '''
        for slave in self.slavePool:
            await slave.waitUntilActive()
            success = await slave.sendConnectToSlaves(self.clusterConfig.getSlaveInfoList())
            if not success:
                self.shutdown()

    def getSlaveConnection(self, branch):
        # TODO:  Support forwarding to multiple connections (for replication)
        check(len(self.shardToSlaves[branch.value]) > 0)
        return self.shardToSlaves[branch.value][0]

    def __logSummary(self):
        for shardId, slaves in enumerate(self.shardToSlaves):
            Logger.info("[{}] is run by slave {}".format(shardId, [s.id for s in slaves]))

    async def __initCluster(self):
        await self.__connectToSlaves()
        self.__logSummary()
        if not self.__hasAllShards():
            Logger.error("Missing some shards. Check cluster config file!")
            return
        await self.__setupSlaveToSlaveConnections()

        self.clusterActiveFuture.set_result(None)

    def start(self):
        self.loop.create_task(self.__initCluster())

    def startAndLoop(self):
        self.start()
        try:
            self.loop.run_until_complete(self.shutdownFuture)
        except KeyboardInterrupt:
            pass

    def shutdown(self):
        # TODO: May set exception and disconnect all slaves
        if not self.shutdownFuture.done():
            self.shutdownFuture.set_result(None)
        if not self.clusterActiveFuture.done():
            self.clusterActiveFuture.set_exception(RuntimeError("failed to start the cluster"))

    def getShutdownFuture(self):
        return self.shutdownFuture

    async def __createRootBlockToMineOrFallbackToMinorBlock(self, address):
        ''' Try to create a root block to mine or fallback to create minor block if failed proof-of-progress '''
        futures = []
        for slave in self.slavePool:
            request = GetUnconfirmedHeadersRequest()
            futures.append(slave.writeRpcRequest(ClusterOp.GET_UNCONFIRMED_HEADERS_REQUEST, request))
        responses = await asyncio.gather(*futures)

        # Slaves may run multiple copies of the same branch
        # branchValue -> HeaderList
        shardIdToHeaderList = dict()
        for response in responses:
            _, response, _ = response
            if response.errorCode != 0:
                return (None, None)
            for headersInfo in response.headersInfoList:
                if headersInfo.branch.getShardSize() != self.__getShardSize():
                    Logger.error("Expect shard size {} got {}".format(
                        self.__getShardSize(), headersInfo.branch.getShardSize()))
                    return (None, None)
                # TODO: check headers are ordered by height
                shardIdToHeaderList[headersInfo.branch.getShardId()] = headersInfo.headerList

        headerList = []
        # check proof of progress
        for shardId in range(self.__getShardSize()):
            headers = shardIdToHeaderList.get(shardId, [])
            headerList.extend(headers)
            if len(headers) < self.env.config.PROOF_OF_PROGRESS_BLOCKS:
                # Fallback to create minor block
                block = await self.__getMinorBlockToMine(Branch.create(self.__getShardSize(), shardId), address)
                return (None, None) if not block else (False, block)

        return (True, self.rootState.createBlockToMine(headerList, address))

    async def __getMinorBlockToMine(self, branch, address):
        request = GetNextBlockToMineRequest(
            branch=branch,
            address=address,
        )
        slave = self.shardToSlaves[branch.getShardId()][0]
        _, response, _ = await slave.writeRpcRequest(ClusterOp.GET_NEXT_BLOCK_TO_MINE_REQUEST, request)
        return response.block if response.errorCode == 0 else None

    async def getNextBlockToMine(self, address, randomizeOutput=True):
        ''' Returns (isRootBlock, block) '''
        futures = []
        for slave in self.slavePool:
            request = GetEcoInfoListRequest()
            futures.append(slave.writeRpcRequest(ClusterOp.GET_ECO_INFO_LIST_REQUEST, request))
        responses = await asyncio.gather(*futures)

        # Slaves may run multiple copies of the same branch
        # We only need one EcoInfo per branch
        # branchValue -> EcoInfo
        branchValueToEcoInfo = dict()
        for response in responses:
            _, response, _ = response
            if response.errorCode != 0:
                return (None, None)
            for ecoInfo in response.ecoInfoList:
                branchValueToEcoInfo[ecoInfo.branch.value] = ecoInfo

        rootCoinbaseAmount = 0
        for branchValue, ecoInfo in branchValueToEcoInfo.items():
            rootCoinbaseAmount += ecoInfo.unconfirmedHeadersCoinbaseAmount
        rootCoinbaseAmount = rootCoinbaseAmount // 2

        branchValueWithMaxEco = 0
        maxEco = rootCoinbaseAmount / self.rootState.getNextBlockDifficulty()

        dupEcoCount = 1
        blockHeight = 0
        for branchValue, ecoInfo in branchValueToEcoInfo.items():
            # TODO: Obtain block reward and tx fee
            eco = ecoInfo.coinbaseAmount / ecoInfo.difficulty
            if eco > maxEco or (eco == maxEco and branchValueWithMaxEco > 0 and blockHeight > ecoInfo.height):
                branchValueWithMaxEco = branchValue
                maxEco = eco
                dupEcoCount = 1
                blockHeight = ecoInfo.height
            elif eco == maxEco and randomizeOutput:
                # The current block with max eco has smaller height, mine the block first
                # This should be only used during bootstrap.
                if branchValueWithMaxEco > 0 and blockHeight < ecoInfo.height:
                    continue
                dupEcoCount += 1
                if random.random() < 1 / dupEcoCount:
                    branchValueWithMaxEco = branchValue
                    maxEco = eco

        if branchValueWithMaxEco == 0:
            return await self.__createRootBlockToMineOrFallbackToMinorBlock(address)

        block = await self.__getMinorBlockToMine(Branch(branchValueWithMaxEco), address)
        return (None, None) if not block else (False, block)

    async def getTransactionCount(self, address):
        shardId = address.getShardId(self.__getShardSize())
        count = await self.shardToSlaves[shardId][0].getTransactionCount(address)
        return Branch.create(self.__getShardSize(), shardId), count

    async def addTransaction(self, tx, branch):
        futures = []
        for slave in self.shardToSlaves[branch.getShardId()]:
            futures.append(slave.addTransaction(tx))

        results = await asyncio.gather(*futures)

        # TODO: broadcast tx to peers

        return all(results)

    def updateRootBlock(self, rBlock):
        self.rootBlockUpdateQueue.append(rBlock)
        if not self.isUpdatingRootBlock:
            self.isUpdatingRootBlock = True
            asyncio.ensure_future(self.updateRooBlockAsync())

    async def updateRooBlockAsync(self):
        ''' Broadcast root block to all shards and add root block locally
        '''
        check(self.isUpdatingRootBlock)
        while len(self.rootBlockUpdateQueue) != 0:
            rBlock = self.rootBlockUpdateQueue.popleft()
            futureList = []
            for shardId in range(self.__getShardSize()):
                branch = Branch(shardId + self.__getShardSize())
                slaveConn = self.getSlaveConnection(branch=branch)
                # TODO: Update switch
                futureList.add(slaveConn.writeRpcRequest(
                    op=ClusterOp.ADD_ROOT_BLOCK_REQUEST,
                    cmd=AddRootBlockRequest(rBlock, False),
                    metadata=ClusterMetadata(branch, bytes(32))))
            resultList = await asyncio.gather(*futureList)
            # TODO: Check resultList
            self.rootState.addBlock(rBlock)
        self.isUpdatingRootBlock = False


def parse_args():
    parser = argparse.ArgumentParser()
    # P2P port
    parser.add_argument(
        "--server_port", default=DEFAULT_ENV.config.P2P_SERVER_PORT, type=int)
    # Local port for JSON-RPC, wallet, etc
    parser.add_argument(
        "--enable_local_server", default=False, type=bool)
    parser.add_argument(
        "--local_port", default=DEFAULT_ENV.config.LOCAL_SERVER_PORT, type=int)
    # Seed host which provides the list of available peers
    parser.add_argument(
        "--seed_host", default=DEFAULT_ENV.config.P2P_SEED_HOST, type=str)
    parser.add_argument(
        "--seed_port", default=DEFAULT_ENV.config.P2P_SEED_PORT, type=int)
    # Node port for intra-cluster RPC
    parser.add_argument(
        "--node_port", default=DEFAULT_ENV.clusterConfig.NODE_PORT, type=int)
    parser.add_argument(
        "--cluster_config", default="cluster_config.json", type=str)
    parser.add_argument("--in_memory_db", default=False)
    parser.add_argument("--db_path", default="./db", type=str)
    parser.add_argument("--log_level", default="info", type=str)
    args = parser.parse_args()

    set_logging_level(args.log_level)

    env = DEFAULT_ENV.copy()
    env.config.P2P_SERVER_PORT = args.server_port
    env.config.P2P_SEED_HOST = args.seed_host
    env.config.P2P_SEED_PORT = args.seed_port
    env.config.LOCAL_SERVER_PORT = args.local_port
    env.config.LOCAL_SERVER_ENABLE = args.enable_local_server
    env.clusterConfig.NODE_PORT = args.node_port
    env.clusterConfig.CONFIG = ClusterConfig(json.load(open(args.cluster_config)))
    if not args.in_memory_db:
        env.db = PersistentDb(path=args.db_path, clean=True)

    return env


def main():
    env = parse_args()
    env.NETWORK_ID = 1  # testnet

    rootState = RootState(env, createGenesis=True)
    network = SimpleNetwork(env, rootState)
    network.start()

    master = MasterServer(env, rootState)

    jsonRpcServer = JSONRPCServer(env, master)
    jsonRpcServer.start()

    master.startAndLoop()

    jsonRpcServer.shutdown()

    Logger.info("Server is shutdown")


if __name__ == '__main__':
    main()