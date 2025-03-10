#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2022 tecnovert
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.

"""
basicswap]$ python tests/basicswap/extended/test_pivx.py

"""

import os
import sys
import json
import time
import shutil
import signal
import logging
import unittest
import threading

import basicswap.config as cfg
from basicswap.basicswap import (
    BasicSwap,
    Coins,
    SwapTypes,
    BidStates,
    TxStates,
)
from basicswap.util import (
    COIN,
)
from basicswap.basicswap_util import (
    TxLockTypes,
)
from basicswap.util.address import (
    toWIF,
)
from basicswap.rpc import (
    callrpc_cli,
    waitForRPC,
)
from basicswap.contrib.key import (
    ECKey,
)
from basicswap.http_server import (
    HttpThread,
)
from tests.basicswap.common import (
    checkForks,
    stopDaemons,
    wait_for_offer,
    wait_for_bid,
    wait_for_bid_tx_state,
    wait_for_in_progress,
    read_json_api,
    TEST_HTTP_HOST,
    TEST_HTTP_PORT,
    BASE_PORT,
    BASE_RPC_PORT,
    BASE_ZMQ_PORT,
    PREFIX_SECRET_KEY_REGTEST,
)
from bin.basicswap_run import startDaemon
from bin.basicswap_prepare import downloadPIVXParams


logger = logging.getLogger()
logger.level = logging.DEBUG
if not len(logger.handlers):
    logger.addHandler(logging.StreamHandler(sys.stdout))

NUM_NODES = 3
PIVX_NODE = 3
BTC_NODE = 4

delay_event = threading.Event()
stop_test = False


def prepareOtherDir(datadir, nodeId, conf_file='pivx.conf'):
    node_dir = os.path.join(datadir, str(nodeId))
    if not os.path.exists(node_dir):
        os.makedirs(node_dir)
    filePath = os.path.join(node_dir, conf_file)

    with open(filePath, 'w+') as fp:
        fp.write('regtest=1\n')
        fp.write('[regtest]\n')
        fp.write('port=' + str(BASE_PORT + nodeId) + '\n')
        fp.write('rpcport=' + str(BASE_RPC_PORT + nodeId) + '\n')

        fp.write('daemon=0\n')
        fp.write('printtoconsole=0\n')
        fp.write('server=1\n')
        fp.write('discover=0\n')
        fp.write('listenonion=0\n')
        fp.write('bind=127.0.0.1\n')
        fp.write('findpeers=0\n')
        fp.write('debug=1\n')
        fp.write('debugexclude=libevent\n')

        fp.write('fallbackfee=0.01\n')
        fp.write('acceptnonstdtxn=0\n')

        if conf_file == 'pivx.conf':
            params_dir = os.path.join(datadir, 'pivx-params')
            downloadPIVXParams(params_dir)
            fp.write(f'paramsdir={params_dir}\n')

        if conf_file == 'bitcoin.conf':
            fp.write('wallet=wallet.dat\n')


def prepareDir(datadir, nodeId, network_key, network_pubkey):
    node_dir = os.path.join(datadir, str(nodeId))
    if not os.path.exists(node_dir):
        os.makedirs(node_dir)
    filePath = os.path.join(node_dir, 'particl.conf')

    with open(filePath, 'w+') as fp:
        fp.write('regtest=1\n')
        fp.write('[regtest]\n')
        fp.write('port=' + str(BASE_PORT + nodeId) + '\n')
        fp.write('rpcport=' + str(BASE_RPC_PORT + nodeId) + '\n')

        fp.write('daemon=0\n')
        fp.write('printtoconsole=0\n')
        fp.write('server=1\n')
        fp.write('discover=0\n')
        fp.write('listenonion=0\n')
        fp.write('bind=127.0.0.1\n')
        fp.write('findpeers=0\n')
        fp.write('debug=1\n')
        fp.write('debugexclude=libevent\n')
        fp.write('zmqpubsmsg=tcp://127.0.0.1:' + str(BASE_ZMQ_PORT + nodeId) + '\n')
        fp.write('wallet=wallet.dat\n')
        fp.write('fallbackfee=0.01\n')

        fp.write('acceptnonstdtxn=0\n')
        fp.write('minstakeinterval=5\n')
        fp.write('smsgsregtestadjust=0\n')

        for i in range(0, NUM_NODES):
            if nodeId == i:
                continue
            fp.write('addnode=127.0.0.1:%d\n' % (BASE_PORT + i))

        if nodeId < 2:
            fp.write('spentindex=1\n')
            fp.write('txindex=1\n')

    basicswap_dir = os.path.join(datadir, str(nodeId), 'basicswap')
    if not os.path.exists(basicswap_dir):
        os.makedirs(basicswap_dir)

    pivxdatadir = os.path.join(datadir, str(PIVX_NODE))
    btcdatadir = os.path.join(datadir, str(BTC_NODE))
    settings_path = os.path.join(basicswap_dir, cfg.CONFIG_FILENAME)
    settings = {
        'debug': True,
        'zmqhost': 'tcp://127.0.0.1',
        'zmqport': BASE_ZMQ_PORT + nodeId,
        'htmlhost': '127.0.0.1',
        'htmlport': 12700 + nodeId,
        'network_key': network_key,
        'network_pubkey': network_pubkey,
        'chainclients': {
            'particl': {
                'connection_type': 'rpc',
                'manage_daemon': False,
                'rpcport': BASE_RPC_PORT + nodeId,
                'datadir': node_dir,
                'bindir': cfg.PARTICL_BINDIR,
                'blocks_confirmed': 2,  # Faster testing
            },
            'pivx': {
                'connection_type': 'rpc',
                'manage_daemon': False,
                'rpcport': BASE_RPC_PORT + PIVX_NODE,
                'datadir': pivxdatadir,
                'bindir': cfg.PIVX_BINDIR,
                'use_csv': False,
                'use_segwit': False,
            },
            'bitcoin': {
                'connection_type': 'rpc',
                'manage_daemon': False,
                'rpcport': BASE_RPC_PORT + BTC_NODE,
                'datadir': btcdatadir,
                'bindir': cfg.BITCOIN_BINDIR,
                'use_segwit': True,
            }
        },
        'check_progress_seconds': 2,
        'check_watched_seconds': 4,
        'check_expired_seconds': 60
    }
    with open(settings_path, 'w') as fp:
        json.dump(settings, fp, indent=4)


def partRpc(cmd, node_id=0):
    return callrpc_cli(cfg.PARTICL_BINDIR, os.path.join(cfg.TEST_DATADIRS, str(node_id)), 'regtest', cmd, cfg.PARTICL_CLI)


def btcRpc(cmd):
    return callrpc_cli(cfg.BITCOIN_BINDIR, os.path.join(cfg.TEST_DATADIRS, str(BTC_NODE)), 'regtest', cmd, cfg.BITCOIN_CLI)


def pivxRpc(cmd):
    return callrpc_cli(cfg.PIVX_BINDIR, os.path.join(cfg.TEST_DATADIRS, str(PIVX_NODE)), 'regtest', cmd, cfg.PIVX_CLI)


def signal_handler(sig, frame):
    global stop_test
    print('signal {} detected.'.format(sig))
    stop_test = True
    delay_event.set()


def run_coins_loop(cls):
    while not stop_test:
        try:
            pivxRpc('generatetoaddress 1 {}'.format(cls.pivx_addr))
            btcRpc('generatetoaddress 1 {}'.format(cls.btc_addr))
        except Exception as e:
            logging.warning('run_coins_loop ' + str(e))
        time.sleep(1.0)


def run_loop(self):
    while not stop_test:
        for c in self.swap_clients:
            c.update()
        time.sleep(1)


def make_part_cli_rpc_func(node_id):
    node_id = node_id

    def rpc_func(method, params=None, wallet=None):
        nonlocal node_id
        cmd = method
        if params:
            for p in params:
                cmd += ' "' + p + '"'
        return partRpc(cmd, node_id)
    return rpc_func


class Test(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        super(Test, cls).setUpClass()

        eckey = ECKey()
        eckey.generate()
        cls.network_key = toWIF(PREFIX_SECRET_KEY_REGTEST, eckey.get_bytes())
        cls.network_pubkey = eckey.get_pubkey().get_bytes().hex()

        if os.path.isdir(cfg.TEST_DATADIRS):
            logging.info('Removing ' + cfg.TEST_DATADIRS)
            for name in os.listdir(cfg.TEST_DATADIRS):
                if name == 'pivx-params':
                    continue
                fullpath = os.path.join(cfg.TEST_DATADIRS, name)
                if os.path.isdir(fullpath):
                    shutil.rmtree(fullpath)
                else:
                    os.remove(fullpath)

        for i in range(NUM_NODES):
            prepareDir(cfg.TEST_DATADIRS, i, cls.network_key, cls.network_pubkey)

        prepareOtherDir(cfg.TEST_DATADIRS, PIVX_NODE)
        prepareOtherDir(cfg.TEST_DATADIRS, BTC_NODE, 'bitcoin.conf')

        cls.daemons = []
        cls.swap_clients = []
        cls.http_threads = []

        btc_data_dir = os.path.join(cfg.TEST_DATADIRS, str(BTC_NODE))
        if os.path.exists(os.path.join(cfg.BITCOIN_BINDIR, 'bitcoin-wallet')):
            callrpc_cli(cfg.BITCOIN_BINDIR, btc_data_dir, 'regtest', '-wallet=wallet.dat create', 'bitcoin-wallet')
        cls.daemons.append(startDaemon(btc_data_dir, cfg.BITCOIN_BINDIR, cfg.BITCOIND))
        logging.info('Started %s %d', cfg.BITCOIND, cls.daemons[-1].pid)
        cls.daemons.append(startDaemon(os.path.join(cfg.TEST_DATADIRS, str(PIVX_NODE)), cfg.PIVX_BINDIR, cfg.PIVXD))
        logging.info('Started %s %d', cfg.PIVXD, cls.daemons[-1].pid)

        for i in range(NUM_NODES):
            data_dir = os.path.join(cfg.TEST_DATADIRS, str(i))
            if os.path.exists(os.path.join(cfg.PARTICL_BINDIR, 'particl-wallet')):
                callrpc_cli(cfg.PARTICL_BINDIR, data_dir, 'regtest', '-wallet=wallet.dat create', 'particl-wallet')
            cls.daemons.append(startDaemon(data_dir, cfg.PARTICL_BINDIR, cfg.PARTICLD))
            logging.info('Started %s %d', cfg.PARTICLD, cls.daemons[-1].pid)

        for i in range(NUM_NODES):
            rpc = make_part_cli_rpc_func(i)
            waitForRPC(rpc)
            if i == 0:
                rpc('extkeyimportmaster', ['abandon baby cabbage dad eager fabric gadget habit ice kangaroo lab absorb'])
            elif i == 1:
                rpc('extkeyimportmaster', ['pact mammal barrel matrix local final lecture chunk wasp survey bid various book strong spread fall ozone daring like topple door fatigue limb olympic', '', 'true'])
                rpc('getnewextaddress', ['lblExtTest'])
                rpc('rescanblockchain')
            else:
                rpc('extkeyimportmaster', [rpc('mnemonic', ['new'])['master']])

            basicswap_dir = os.path.join(os.path.join(cfg.TEST_DATADIRS, str(i)), 'basicswap')
            settings_path = os.path.join(basicswap_dir, cfg.CONFIG_FILENAME)
            with open(settings_path) as fs:
                settings = json.load(fs)
            fp = open(os.path.join(basicswap_dir, 'basicswap.log'), 'w')
            cls.swap_clients.append(BasicSwap(fp, basicswap_dir, settings, 'regtest', log_name='BasicSwap{}'.format(i)))
            cls.swap_clients[-1].setDaemonPID(Coins.BTC, cls.daemons[0].pid)
            cls.swap_clients[-1].setDaemonPID(Coins.PIVX, cls.daemons[1].pid)
            cls.swap_clients[-1].setDaemonPID(Coins.PART, cls.daemons[2 + i].pid)
            cls.swap_clients[-1].start()

            t = HttpThread(cls.swap_clients[i].fp, TEST_HTTP_HOST, TEST_HTTP_PORT + i, False, cls.swap_clients[i])
            cls.http_threads.append(t)
            t.start()

        waitForRPC(pivxRpc)
        num_blocks = 1352  # CHECKLOCKTIMEVERIFY soft-fork activates at (regtest) block height 1351.
        logging.info('Mining %d pivx blocks', num_blocks)
        cls.pivx_addr = pivxRpc('getnewaddress mining_addr')
        pivxRpc('generatetoaddress {} {}'.format(num_blocks, cls.pivx_addr))

        ro = pivxRpc('getblockchaininfo')
        try:
            assert (ro['bip9_softforks']['csv']['status'] == 'active')
        except Exception:
            logging.info('pivx: csv is not active')
        try:
            assert (ro['bip9_softforks']['segwit']['status'] == 'active')
        except Exception:
            logging.info('pivx: segwit is not active')

        waitForRPC(btcRpc)
        cls.btc_addr = btcRpc('getnewaddress mining_addr bech32')
        logging.info('Mining %d Bitcoin blocks to %s', num_blocks, cls.btc_addr)
        btcRpc('generatetoaddress {} {}'.format(num_blocks, cls.btc_addr))

        ro = btcRpc('getblockchaininfo')
        checkForks(ro)

        ro = pivxRpc('getwalletinfo')
        print('pivxRpc', ro)

        signal.signal(signal.SIGINT, signal_handler)
        cls.update_thread = threading.Thread(target=run_loop, args=(cls,))
        cls.update_thread.start()

        cls.coins_update_thread = threading.Thread(target=run_coins_loop, args=(cls,))
        cls.coins_update_thread.start()

        # Wait for height, or sequencelock is thrown off by genesis blocktime
        num_blocks = 3
        logging.info('Waiting for Particl chain height %d', num_blocks)
        for i in range(60):
            particl_blocks = cls.swap_clients[0].callrpc('getblockchaininfo')['blocks']
            print('particl_blocks', particl_blocks)
            if particl_blocks >= num_blocks:
                break
            delay_event.wait(1)
        assert (particl_blocks >= num_blocks)

    @classmethod
    def tearDownClass(cls):
        global stop_test
        logging.info('Finalising')
        stop_test = True
        cls.update_thread.join()
        cls.coins_update_thread.join()
        for t in cls.http_threads:
            t.stop()
            t.join()
        for c in cls.swap_clients:
            c.finalise()
            c.fp.close()

        stopDaemons(cls.daemons)

        super(Test, cls).tearDownClass()

    def test_02_part_pivx(self):
        logging.info('---------- Test PART to PIVX')
        swap_clients = self.swap_clients

        offer_id = swap_clients[0].postOffer(Coins.PART, Coins.PIVX, 100 * COIN, 0.1 * COIN, 100 * COIN, SwapTypes.SELLER_FIRST, TxLockTypes.ABS_LOCK_TIME)

        wait_for_offer(delay_event, swap_clients[1], offer_id)
        offers = swap_clients[1].listOffers()
        assert (len(offers) == 1)
        for offer in offers:
            if offer.offer_id == offer_id:
                bid_id = swap_clients[1].postBid(offer_id, offer.amount_from)

        wait_for_bid(delay_event, swap_clients[0], bid_id)

        swap_clients[0].acceptBid(bid_id)

        wait_for_in_progress(delay_event, swap_clients[1], bid_id, sent=True)

        wait_for_bid(delay_event, swap_clients[0], bid_id, BidStates.SWAP_COMPLETED, wait_for=60)
        wait_for_bid(delay_event, swap_clients[1], bid_id, BidStates.SWAP_COMPLETED, sent=True, wait_for=60)

        js_0 = read_json_api(1800)
        js_1 = read_json_api(1801)
        assert (js_0['num_swapping'] == 0 and js_0['num_watched_outputs'] == 0)
        assert (js_1['num_swapping'] == 0 and js_1['num_watched_outputs'] == 0)

    def test_03_pivx_part(self):
        logging.info('---------- Test PIVX to PART')
        swap_clients = self.swap_clients

        offer_id = swap_clients[1].postOffer(Coins.PIVX, Coins.PART, 10 * COIN, 9.0 * COIN, 10 * COIN, SwapTypes.SELLER_FIRST, TxLockTypes.ABS_LOCK_TIME)

        wait_for_offer(delay_event, swap_clients[0], offer_id)
        offers = swap_clients[0].listOffers()
        for offer in offers:
            if offer.offer_id == offer_id:
                bid_id = swap_clients[0].postBid(offer_id, offer.amount_from)

        wait_for_bid(delay_event, swap_clients[1], bid_id)
        swap_clients[1].acceptBid(bid_id)

        wait_for_in_progress(delay_event, swap_clients[0], bid_id, sent=True)

        wait_for_bid(delay_event, swap_clients[0], bid_id, BidStates.SWAP_COMPLETED, sent=True, wait_for=60)
        wait_for_bid(delay_event, swap_clients[1], bid_id, BidStates.SWAP_COMPLETED, wait_for=60)

        js_0 = read_json_api(1800)
        js_1 = read_json_api(1801)
        assert (js_0['num_swapping'] == 0 and js_0['num_watched_outputs'] == 0)
        assert (js_1['num_swapping'] == 0 and js_1['num_watched_outputs'] == 0)

    def test_04_pivx_btc(self):
        logging.info('---------- Test PIVX to BTC')
        swap_clients = self.swap_clients

        offer_id = swap_clients[0].postOffer(Coins.PIVX, Coins.BTC, 10 * COIN, 0.1 * COIN, 10 * COIN, SwapTypes.SELLER_FIRST, TxLockTypes.ABS_LOCK_TIME)

        wait_for_offer(delay_event, swap_clients[1], offer_id)
        offers = swap_clients[1].listOffers()
        for offer in offers:
            if offer.offer_id == offer_id:
                bid_id = swap_clients[1].postBid(offer_id, offer.amount_from)

        wait_for_bid(delay_event, swap_clients[0], bid_id)
        swap_clients[0].acceptBid(bid_id)

        wait_for_in_progress(delay_event, swap_clients[1], bid_id, sent=True)

        wait_for_bid(delay_event, swap_clients[0], bid_id, BidStates.SWAP_COMPLETED, wait_for=60)
        wait_for_bid(delay_event, swap_clients[1], bid_id, BidStates.SWAP_COMPLETED, sent=True, wait_for=60)

        js_0bid = read_json_api(1800, 'bids/{}'.format(bid_id.hex()))

        js_0 = read_json_api(1800)
        js_1 = read_json_api(1801)

        assert (js_0['num_swapping'] == 0 and js_0['num_watched_outputs'] == 0)
        assert (js_1['num_swapping'] == 0 and js_1['num_watched_outputs'] == 0)

    def test_05_refund(self):
        # Seller submits initiate txn, buyer doesn't respond
        logging.info('---------- Test refund, PIVX to BTC')
        swap_clients = self.swap_clients

        offer_id = swap_clients[0].postOffer(Coins.PIVX, Coins.BTC, 10 * COIN, 0.1 * COIN, 10 * COIN, SwapTypes.SELLER_FIRST,
                                             TxLockTypes.ABS_LOCK_BLOCKS, 10)

        wait_for_offer(delay_event, swap_clients[1], offer_id)
        offers = swap_clients[1].listOffers()
        for offer in offers:
            if offer.offer_id == offer_id:
                bid_id = swap_clients[1].postBid(offer_id, offer.amount_from)

        wait_for_bid(delay_event, swap_clients[0], bid_id)
        swap_clients[1].abandonBid(bid_id)
        swap_clients[0].acceptBid(bid_id)

        wait_for_bid(delay_event, swap_clients[0], bid_id, BidStates.SWAP_COMPLETED, wait_for=60)
        wait_for_bid(delay_event, swap_clients[1], bid_id, BidStates.BID_ABANDONED, sent=True, wait_for=60)

        js_0 = read_json_api(1800)
        js_1 = read_json_api(1801)
        assert (js_0['num_swapping'] == 0 and js_0['num_watched_outputs'] == 0)
        assert (js_1['num_swapping'] == 0 and js_1['num_watched_outputs'] == 0)

    def test_06_self_bid(self):
        logging.info('---------- Test same client, BTC to PIVX')
        swap_clients = self.swap_clients

        js_0_before = read_json_api(1800)

        offer_id = swap_clients[0].postOffer(Coins.PIVX, Coins.BTC, 10 * COIN, 10 * COIN, 10 * COIN, SwapTypes.SELLER_FIRST, TxLockTypes.ABS_LOCK_TIME)

        wait_for_offer(delay_event, swap_clients[0], offer_id)
        offers = swap_clients[0].listOffers()
        for offer in offers:
            if offer.offer_id == offer_id:
                bid_id = swap_clients[0].postBid(offer_id, offer.amount_from)

        wait_for_bid(delay_event, swap_clients[0], bid_id)
        swap_clients[0].acceptBid(bid_id)

        wait_for_bid_tx_state(delay_event, swap_clients[0], bid_id, TxStates.TX_REDEEMED, TxStates.TX_REDEEMED, wait_for=60)
        wait_for_bid(delay_event, swap_clients[0], bid_id, BidStates.SWAP_COMPLETED, wait_for=60)

        js_0 = read_json_api(1800)
        assert (js_0['num_swapping'] == 0 and js_0['num_watched_outputs'] == 0)
        assert (js_0['num_recv_bids'] == js_0_before['num_recv_bids'] + 1 and js_0['num_sent_bids'] == js_0_before['num_sent_bids'] + 1)

    def test_07_error(self):
        logging.info('---------- Test error, BTC to PIVX, set fee above bid value')
        swap_clients = self.swap_clients

        js_0_before = read_json_api(1800)

        offer_id = swap_clients[0].postOffer(Coins.PIVX, Coins.BTC, 0.001 * COIN, 1.0 * COIN, 0.001 * COIN, SwapTypes.SELLER_FIRST, TxLockTypes.ABS_LOCK_TIME)

        wait_for_offer(delay_event, swap_clients[0], offer_id)
        offers = swap_clients[0].listOffers()
        for offer in offers:
            if offer.offer_id == offer_id:
                bid_id = swap_clients[0].postBid(offer_id, offer.amount_from)

        wait_for_bid(delay_event, swap_clients[0], bid_id)
        swap_clients[0].acceptBid(bid_id)
        swap_clients[0].getChainClientSettings(Coins.BTC)['override_feerate'] = 10.0
        swap_clients[0].getChainClientSettings(Coins.PIVX)['override_feerate'] = 10.0
        wait_for_bid(delay_event, swap_clients[0], bid_id, BidStates.BID_ERROR, wait_for=60)

    def pass_99_delay(self):
        global stop_test
        logging.info('Delay')
        for i in range(60 * 5):
            if stop_test:
                break
            time.sleep(1)
            print('delay', i)
        stop_test = True


if __name__ == '__main__':
    unittest.main()
