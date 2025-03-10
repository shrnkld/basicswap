#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2020-2022 tecnovert
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.

import json
import time
import base64
import hashlib
import logging
import traceback
from io import BytesIO
from basicswap.contrib.test_framework import segwit_addr

from basicswap.util import (
    dumpj,
    ensure,
    make_int,
    b2h, i2b, b2i, i2h)
from basicswap.util.ecc import (
    ep,
    pointToCPK, CPKToPoint,
    getSecretInt)
from basicswap.util.script import (
    decodeScriptNum,
    getCompactSizeLen,
    SerialiseNumCompact,
    getWitnessElementLen,
)
from basicswap.util.address import (
    toWIF,
    b58encode,
    decodeWif,
    decodeAddress,
    pubkeyToAddress,
)
from coincurve.keys import (
    PrivateKey,
    PublicKey)
from coincurve.dleag import (
    verify_secp256k1_point)
from coincurve.ecdsaotves import (
    ecdsaotves_enc_sign,
    ecdsaotves_enc_verify,
    ecdsaotves_dec_sig,
    ecdsaotves_rec_enc_key)

from basicswap.contrib.test_framework.messages import (
    COIN,
    COutPoint,
    CTransaction,
    CTxIn,
    CTxInWitness,
    CTxOut,
    FromHex)

from basicswap.contrib.test_framework.script import (
    CScript, CScriptOp,
    OP_IF, OP_ELSE, OP_ENDIF,
    OP_0, OP_2,
    OP_CHECKSIG,
    OP_CHECKMULTISIG,
    OP_CHECKSEQUENCEVERIFY,
    OP_DROP,
    SIGHASH_ALL,
    SegwitV0SignatureHash,
    hash160)

from basicswap.basicswap_util import (
    TxLockTypes)

from basicswap.chainparams import CoinInterface, Coins
from basicswap.rpc import make_rpc_func, openrpc


SEQUENCE_LOCKTIME_GRANULARITY = 9  # 512 seconds
SEQUENCE_LOCKTIME_TYPE_FLAG = (1 << 22)
SEQUENCE_LOCKTIME_MASK = 0x0000ffff


def ensure_op(v, err_string='Bad opcode'):
    ensure(v, err_string)


def findOutput(tx, script_pk):
    for i in range(len(tx.vout)):
        if tx.vout[i].scriptPubKey == script_pk:
            return i
    return None


def find_vout_for_address_from_txobj(tx_obj, addr):
    """
    Locate the vout index of the given transaction sending to the
    given address. Raises runtime error exception if not found.
    """
    for i in range(len(tx_obj["vout"])):
        scriptPubKey = tx_obj["vout"][i]["scriptPubKey"]
        if "addresses" in scriptPubKey:
            if any([addr == a for a in scriptPubKey["addresses"]]):
                return i
        elif "address" in scriptPubKey:
            if addr == scriptPubKey["address"]:
                return i
    raise RuntimeError("Vout not found for address: txid={}, addr={}".format(tx_obj['txid'], addr))


class BTCInterface(CoinInterface):
    @staticmethod
    def coin_type():
        return Coins.BTC

    @staticmethod
    def COIN():
        return COIN

    @staticmethod
    def exp() -> int:
        return 8

    @staticmethod
    def nbk() -> int:
        return 32

    @staticmethod
    def nbK() -> int:  # No. of bytes requires to encode a public key
        return 33

    @staticmethod
    def witnessScaleFactor() -> int:
        return 4

    @staticmethod
    def txVersion() -> int:
        return 2

    @staticmethod
    def getTxOutputValue(tx):
        rv = 0
        for output in tx.vout:
            rv += output.nValue
        return rv

    @staticmethod
    def compareFeeRates(a, b) -> bool:
        return abs(a - b) < 20

    @staticmethod
    def xmr_swap_alock_spend_tx_vsize() -> int:
        return 147

    @staticmethod
    def txoType():
        return CTxOut

    @staticmethod
    def getExpectedSequence(lockType, lockVal):
        assert (lockVal >= 1), 'Bad lockVal'
        if lockType == TxLockTypes.SEQUENCE_LOCK_BLOCKS:
            return lockVal
        if lockType == TxLockTypes.SEQUENCE_LOCK_TIME:
            secondsLocked = lockVal
            # Ensure the locked time is never less than lockVal
            if secondsLocked % (1 << SEQUENCE_LOCKTIME_GRANULARITY) != 0:
                secondsLocked += (1 << SEQUENCE_LOCKTIME_GRANULARITY)
            secondsLocked >>= SEQUENCE_LOCKTIME_GRANULARITY
            return secondsLocked | SEQUENCE_LOCKTIME_TYPE_FLAG
        raise ValueError('Unknown lock type')

    @staticmethod
    def decodeSequence(lock_value):
        # Return the raw value
        if lock_value & SEQUENCE_LOCKTIME_TYPE_FLAG:
            return (lock_value & SEQUENCE_LOCKTIME_MASK) << SEQUENCE_LOCKTIME_GRANULARITY
        return lock_value & SEQUENCE_LOCKTIME_MASK

    def __init__(self, coin_settings, network, swap_client=None):
        super().__init__(network)
        self._rpc_host = coin_settings.get('rpchost', '127.0.0.1')
        self._rpcport = coin_settings['rpcport']
        self._rpcauth = coin_settings['rpcauth']
        self.rpc_callback = make_rpc_func(self._rpcport, self._rpcauth, host=self._rpc_host)
        self.blocks_confirmed = coin_settings['blocks_confirmed']
        self.setConfTarget(coin_settings['conf_target'])
        self._use_segwit = coin_settings['use_segwit']
        self._connection_type = coin_settings['connection_type']
        self._sc = swap_client
        self._log = self._sc.log if self._sc and self._sc.log else logging

    def using_segwit(self):
        return self._use_segwit

    def get_connection_type(self):
        return self._connection_type

    def open_rpc(self, wallet=None):
        return openrpc(self._rpcport, self._rpcauth, wallet=wallet, host=self._rpc_host)

    def json_request(self, rpc_conn, method, params):
        try:
            v = rpc_conn.json_request(method, params)
            r = json.loads(v.decode('utf-8'))
        except Exception as ex:
            traceback.print_exc()
            raise ValueError('RPC Server Error ' + str(ex))

        if 'error' in r and r['error'] is not None:
            raise ValueError('RPC error ' + str(r['error']))

        return r['result']

    def close_rpc(self, rpc_conn):
        rpc_conn.close()

    def setConfTarget(self, new_conf_target):
        ensure(new_conf_target >= 1 and new_conf_target < 33, 'Invalid conf_target value')
        self._conf_target = new_conf_target

    def testDaemonRPC(self, with_wallet=True):
        if with_wallet:
            self.rpc_callback('getwalletinfo', [])
        else:
            self.rpc_callback('getblockchaininfo', [])

    def getDaemonVersion(self):
        return self.rpc_callback('getnetworkinfo')['version']

    def getBlockchainInfo(self):
        return self.rpc_callback('getblockchaininfo')

    def getChainHeight(self):
        return self.rpc_callback('getblockcount')

    def getMempoolTx(self, txid):
        return self.rpc_callback('getrawtransaction', [txid.hex()])

    def getBlockHeaderFromHeight(self, height):
        block_hash = self.rpc_callback('getblockhash', [height])
        return self.rpc_callback('getblockheader', [block_hash])

    def getBlockHeader(self, block_hash):
        return self.rpc_callback('getblockheader', [block_hash])

    def getBlockHeaderAt(self, time, block_after=False):
        blockchaininfo = self.rpc_callback('getblockchaininfo')
        last_block_header = self.rpc_callback('getblockheader', [blockchaininfo['bestblockhash']])

        max_tries = 5000
        for i in range(max_tries):
            prev_block_header = self.rpc_callback('getblock', [last_block_header['previousblockhash']])
            if prev_block_header['time'] <= time:
                return last_block_header if block_after else prev_block_header

            last_block_header = prev_block_header
        raise ValueError(f'Block header not found at time: {time}')

    def initialiseWallet(self, key_bytes):
        key_wif = self.encodeKey(key_bytes)

        try:
            self.rpc_callback('sethdseed', [True, key_wif])
        except Exception as e:
            # <  0.21: Cannot set a new HD seed while still in Initial Block Download.
            self._log.error('sethdseed failed: {}'.format(str(e)))

    def getWalletInfo(self):
        return self.rpc_callback('getwalletinfo')

    def walletRestoreHeight(self):
        return self._restore_height

    def getWalletRestoreHeight(self):
        start_time = self.rpc_callback('getwalletinfo')['keypoololdest']

        blockchaininfo = self.rpc_callback('getblockchaininfo')
        best_block = blockchaininfo['bestblockhash']

        chain_synced = round(blockchaininfo['verificationprogress'], 3)
        if chain_synced < 1.0:
            raise ValueError('{} chain isn\'t synced.'.format(self.coin_name()))

        self._log.debug('Finding block at time: {}'.format(start_time))

        rpc_conn = self.open_rpc()
        try:
            block_hash = best_block
            while True:
                block_header = self.json_request(rpc_conn, 'getblockheader', [block_hash])
                if block_header['time'] < start_time:
                    return block_header['height']
                block_hash = block_header['previousblockhash']
        finally:
            self.close_rpc(rpc_conn)

    def getWalletSeedID(self):
        return self.rpc_callback('getwalletinfo')['hdseedid']

    def getNewAddress(self, use_segwit, label='swap_receive'):
        args = [label]
        if use_segwit:
            args.append('bech32')
        return self.rpc_callback('getnewaddress', args)

    def get_fee_rate(self, conf_target=2):
        try:
            fee_rate = self.rpc_callback('estimatesmartfee', [conf_target])['feerate']
            assert (fee_rate > 0.0), 'Non positive feerate'
            return fee_rate, 'estimatesmartfee'
        except Exception:
            try:
                fee_rate = self.rpc_callback('getwalletinfo')['paytxfee']
                assert (fee_rate > 0.0), 'Non positive feerate'
                return fee_rate, 'paytxfee'
            except Exception:
                return self.rpc_callback('getnetworkinfo')['relayfee'], 'relayfee'

    def isSegwitAddress(self, address):
        return address.startswith(self.chainparams_network()['hrp'] + '1')

    def decodeAddress(self, address):
        bech32_prefix = self.chainparams_network()['hrp']
        if address.startswith(bech32_prefix + '1'):
            return bytes(segwit_addr.decode(bech32_prefix, address)[1])
        return decodeAddress(address)[1:]

    def pubkey_to_segwit_address(self, pk):
        bech32_prefix = self.chainparams_network()['hrp']
        version = 0
        pkh = hash160(pk)
        return segwit_addr.encode(bech32_prefix, version, pkh)

    def pkh_to_address(self, pkh):
        # pkh is hash160(pk)
        assert (len(pkh) == 20)
        prefix = self.chainparams_network()['pubkey_address']
        data = bytes((prefix,)) + pkh
        checksum = hashlib.sha256(hashlib.sha256(data).digest()).digest()
        return b58encode(data + checksum[0:4])

    def encode_p2wsh(self, script):
        bech32_prefix = self.chainparams_network()['hrp']
        version = 0
        program = script[2:]  # strip version and length
        return segwit_addr.encode(bech32_prefix, version, program)

    def encode_p2sh(self, script):
        return pubkeyToAddress(self.chainparams_network()['script_address'], script)

    def pubkey_to_address(self, pk):
        assert (len(pk) == 33)
        return self.pkh_to_address(hash160(pk))

    def getNewSecretKey(self):
        return getSecretInt()

    def getPubkey(self, privkey):
        return PublicKey.from_secret(privkey).format()

    def getAddressHashFromKey(self, key):
        pk = self.getPubkey(key)
        return hash160(pk)

    def verifyKey(self, k):
        i = b2i(k)
        return (i < ep.o and i > 0)

    def verifyPubkey(self, pubkey_bytes):
        return verify_secp256k1_point(pubkey_bytes)

    def encodeKey(self, key_bytes):
        wif_prefix = self.chainparams_network()['key_prefix']
        return toWIF(wif_prefix, key_bytes)

    def encodePubkey(self, pk):
        return pointToCPK(pk)

    def decodePubkey(self, pke):
        return CPKToPoint(pke)

    def decodeKey(self, k):
        return decodeWif(k)

    def sumKeys(self, ka, kb):
        # TODO: Add to coincurve
        return i2b((b2i(ka) + b2i(kb)) % ep.o)

    def sumPubkeys(self, Ka, Kb):
        return PublicKey.combine_keys([PublicKey(Ka), PublicKey(Kb)]).format()

    def getScriptForPubkeyHash(self, pkh):
        return CScript([OP_0, pkh])

    def extractScriptLockScriptValues(self, script_bytes):
        script_len = len(script_bytes)
        ensure(script_len == 71, 'Bad script length')
        o = 0
        ensure_op(script_bytes[o] == OP_2)
        ensure_op(script_bytes[o + 1] == 33)
        o += 2
        pk1 = script_bytes[o: o + 33]
        o += 33
        ensure_op(script_bytes[o] == 33)
        o += 1
        pk2 = script_bytes[o: o + 33]
        o += 33
        ensure_op(script_bytes[o] == OP_2)
        ensure_op(script_bytes[o + 1] == OP_CHECKMULTISIG)

        return pk1, pk2

    def genScriptLockTxScript(self, Kal, Kaf):
        Kal_enc = Kal if len(Kal) == 33 else self.encodePubkey(Kal)
        Kaf_enc = Kaf if len(Kaf) == 33 else self.encodePubkey(Kaf)

        return CScript([2, Kal_enc, Kaf_enc, 2, CScriptOp(OP_CHECKMULTISIG)])

    def createScriptLockTx(self, value, Kal, Kaf, vkbv=None):
        script = self.genScriptLockTxScript(Kal, Kaf)
        tx = CTransaction()
        tx.nVersion = self.txVersion()
        tx.vout.append(self.txoType()(value, self.getScriptDest(script)))

        return tx.serialize(), script

    def fundScriptLockTx(self, tx_bytes, feerate, vkbv=None):
        return self.fundTx(tx_bytes, feerate)

    def extractScriptLockRefundScriptValues(self, script_bytes):
        script_len = len(script_bytes)
        ensure(script_len > 73, 'Bad script length')
        ensure_op(script_bytes[0] == OP_IF)
        ensure_op(script_bytes[1] == OP_2)
        ensure_op(script_bytes[2] == 33)
        pk1 = script_bytes[3: 3 + 33]
        ensure_op(script_bytes[36] == 33)
        pk2 = script_bytes[37: 37 + 33]
        ensure_op(script_bytes[70] == OP_2)
        ensure_op(script_bytes[71] == OP_CHECKMULTISIG)
        ensure_op(script_bytes[72] == OP_ELSE)
        o = 73
        csv_val, nb = decodeScriptNum(script_bytes, o)
        o += nb

        ensure(script_len == o + 5 + 33, 'Bad script length')  # Fails if script too long
        ensure_op(script_bytes[o] == OP_CHECKSEQUENCEVERIFY)
        o += 1
        ensure_op(script_bytes[o] == OP_DROP)
        o += 1
        ensure_op(script_bytes[o] == 33)
        o += 1
        pk3 = script_bytes[o: o + 33]
        o += 33
        ensure_op(script_bytes[o] == OP_CHECKSIG)
        o += 1
        ensure_op(script_bytes[o] == OP_ENDIF)

        return pk1, pk2, csv_val, pk3

    def genScriptLockRefundTxScript(self, Kal, Kaf, csv_val):

        Kal_enc = Kal if len(Kal) == 33 else self.encodePubkey(Kal)
        Kaf_enc = Kaf if len(Kaf) == 33 else self.encodePubkey(Kaf)

        return CScript([
            CScriptOp(OP_IF),
            2, Kal_enc, Kaf_enc, 2, CScriptOp(OP_CHECKMULTISIG),
            CScriptOp(OP_ELSE),
            csv_val, CScriptOp(OP_CHECKSEQUENCEVERIFY), CScriptOp(OP_DROP),
            Kaf_enc, CScriptOp(OP_CHECKSIG),
            CScriptOp(OP_ENDIF)])

    def createScriptLockRefundTx(self, tx_lock_bytes, script_lock, Kal, Kaf, lock1_value, csv_val, tx_fee_rate, vkbv=None):
        tx_lock = CTransaction()
        tx_lock = FromHex(tx_lock, tx_lock_bytes.hex())

        output_script = CScript([OP_0, hashlib.sha256(script_lock).digest()])
        locked_n = findOutput(tx_lock, output_script)
        ensure(locked_n is not None, 'Output not found in tx')
        locked_coin = tx_lock.vout[locked_n].nValue

        tx_lock.rehash()
        tx_lock_id_int = tx_lock.sha256

        refund_script = self.genScriptLockRefundTxScript(Kal, Kaf, csv_val)
        tx = CTransaction()
        tx.nVersion = self.txVersion()
        tx.vin.append(CTxIn(COutPoint(tx_lock_id_int, locked_n), nSequence=lock1_value))
        tx.vout.append(self.txoType()(locked_coin, CScript([OP_0, hashlib.sha256(refund_script).digest()])))

        dummy_witness_stack = self.getScriptLockTxDummyWitness(script_lock)
        witness_bytes = self.getWitnessStackSerialisedLength(dummy_witness_stack)
        vsize = self.getTxVSize(tx, add_witness_bytes=witness_bytes)
        pay_fee = int(tx_fee_rate * vsize // 1000)
        tx.vout[0].nValue = locked_coin - pay_fee

        tx.rehash()
        self._log.info('createScriptLockRefundTx %s:\n    fee_rate, vsize, fee: %ld, %ld, %ld.',
                       i2h(tx.sha256), tx_fee_rate, vsize, pay_fee)

        return tx.serialize(), refund_script, tx.vout[0].nValue

    def createScriptLockRefundSpendTx(self, tx_lock_refund_bytes, script_lock_refund, pkh_refund_to, tx_fee_rate, vkbv=None):
        # Returns the coinA locked coin to the leader
        # The follower will sign the multisig path with a signature encumbered by the leader's coinB spend pubkey
        # If the leader publishes the decrypted signature the leader's coinB spend privatekey will be revealed to the follower

        tx_lock_refund = self.loadTx(tx_lock_refund_bytes)

        output_script = CScript([OP_0, hashlib.sha256(script_lock_refund).digest()])
        locked_n = findOutput(tx_lock_refund, output_script)
        ensure(locked_n is not None, 'Output not found in tx')
        locked_coin = tx_lock_refund.vout[locked_n].nValue

        tx_lock_refund.rehash()
        tx_lock_refund_hash_int = tx_lock_refund.sha256

        tx = CTransaction()
        tx.nVersion = self.txVersion()
        tx.vin.append(CTxIn(COutPoint(tx_lock_refund_hash_int, locked_n), nSequence=0))

        tx.vout.append(self.txoType()(locked_coin, self.getScriptForPubkeyHash(pkh_refund_to)))

        dummy_witness_stack = self.getScriptLockRefundSpendTxDummyWitness(script_lock_refund)
        witness_bytes = self.getWitnessStackSerialisedLength(dummy_witness_stack)
        vsize = self.getTxVSize(tx, add_witness_bytes=witness_bytes)
        pay_fee = int(tx_fee_rate * vsize // 1000)
        tx.vout[0].nValue = locked_coin - pay_fee

        tx.rehash()
        self._log.info('createScriptLockRefundSpendTx %s:\n    fee_rate, vsize, fee: %ld, %ld, %ld.',
                       i2h(tx.sha256), tx_fee_rate, vsize, pay_fee)

        return tx.serialize()

    def createScriptLockRefundSpendToFTx(self, tx_lock_refund_bytes, script_lock_refund, pkh_dest, tx_fee_rate, vkbv=None):
        # lock refund swipe tx
        # Sends the coinA locked coin to the follower

        tx_lock_refund = self.loadTx(tx_lock_refund_bytes)

        output_script = CScript([OP_0, hashlib.sha256(script_lock_refund).digest()])
        locked_n = findOutput(tx_lock_refund, output_script)
        ensure(locked_n is not None, 'Output not found in tx')
        locked_coin = tx_lock_refund.vout[locked_n].nValue

        A, B, lock2_value, C = self.extractScriptLockRefundScriptValues(script_lock_refund)

        tx_lock_refund.rehash()
        tx_lock_refund_hash_int = tx_lock_refund.sha256

        tx = CTransaction()
        tx.nVersion = self.txVersion()
        tx.vin.append(CTxIn(COutPoint(tx_lock_refund_hash_int, locked_n), nSequence=lock2_value))

        tx.vout.append(self.txoType()(locked_coin, self.getScriptForPubkeyHash(pkh_dest)))

        dummy_witness_stack = self.getScriptLockRefundSwipeTxDummyWitness(script_lock_refund)
        witness_bytes = self.getWitnessStackSerialisedLength(dummy_witness_stack)
        vsize = self.getTxVSize(tx, add_witness_bytes=witness_bytes)
        pay_fee = int(tx_fee_rate * vsize // 1000)
        tx.vout[0].nValue = locked_coin - pay_fee

        tx.rehash()
        self._log.info('createScriptLockRefundSpendToFTx %s:\n    fee_rate, vsize, fee: %ld, %ld, %ld.',
                       i2h(tx.sha256), tx_fee_rate, vsize, pay_fee)

        return tx.serialize()

    def createScriptLockSpendTx(self, tx_lock_bytes, script_lock, pkh_dest, tx_fee_rate, vkbv=None):
        tx_lock = self.loadTx(tx_lock_bytes)
        output_script = CScript([OP_0, hashlib.sha256(script_lock).digest()])
        locked_n = findOutput(tx_lock, output_script)
        ensure(locked_n is not None, 'Output not found in tx')
        locked_coin = tx_lock.vout[locked_n].nValue

        tx_lock.rehash()
        tx_lock_id_int = tx_lock.sha256

        tx = CTransaction()
        tx.nVersion = self.txVersion()
        tx.vin.append(CTxIn(COutPoint(tx_lock_id_int, locked_n)))

        tx.vout.append(self.txoType()(locked_coin, self.getScriptForPubkeyHash(pkh_dest)))

        dummy_witness_stack = self.getScriptLockTxDummyWitness(script_lock)
        witness_bytes = self.getWitnessStackSerialisedLength(dummy_witness_stack)
        vsize = self.getTxVSize(tx, add_witness_bytes=witness_bytes)
        pay_fee = int(tx_fee_rate * vsize // 1000)
        tx.vout[0].nValue = locked_coin - pay_fee

        tx.rehash()
        self._log.info('createScriptLockSpendTx %s:\n    fee_rate, vsize, fee: %ld, %ld, %ld.',
                       i2h(tx.sha256), tx_fee_rate, vsize, pay_fee)

        return tx.serialize()

    def verifyLockTx(self, tx_bytes, script_out,
                     swap_value,
                     Kal, Kaf,
                     feerate,
                     check_lock_tx_inputs, vkbv=None):
        # Verify:
        #

        # Not necessary to check the lock txn is mineable, as protocol will wait for it to confirm
        # However by checking early we can avoid wasting time processing unmineable txns
        # Check fee is reasonable

        tx = self.loadTx(tx_bytes)
        txid = self.getTxid(tx)
        self._log.info('Verifying lock tx: {}.'.format(b2h(txid)))

        ensure(tx.nVersion == self.txVersion(), 'Bad version')
        ensure(tx.nLockTime == 0, 'Bad nLockTime')  # TODO match txns created by cores

        script_pk = CScript([OP_0, hashlib.sha256(script_out).digest()])
        locked_n = findOutput(tx, script_pk)
        ensure(locked_n is not None, 'Output not found in tx')
        locked_coin = tx.vout[locked_n].nValue

        # Check value
        ensure(locked_coin == swap_value, 'Bad locked value')

        # Check script
        A, B = self.extractScriptLockScriptValues(script_out)
        ensure(A == Kal, 'Bad script pubkey')
        ensure(B == Kaf, 'Bad script pubkey')

        if check_lock_tx_inputs:
            # TODO: Check that inputs are unspent
            # Verify fee rate
            inputs_value = 0
            add_bytes = 0
            add_witness_bytes = getCompactSizeLen(len(tx.vin))
            for pi in tx.vin:
                ptx = self.rpc_callback('getrawtransaction', [i2h(pi.prevout.hash), True])
                prevout = ptx['vout'][pi.prevout.n]
                inputs_value += make_int(prevout['value'])

                prevout_type = prevout['scriptPubKey']['type']
                if prevout_type == 'witness_v0_keyhash':
                    add_witness_bytes += 107  # sig 72, pk 33 and 2 size bytes
                    add_witness_bytes += getCompactSizeLen(107)
                else:
                    # Assume P2PKH, TODO more types
                    add_bytes += 107  # OP_PUSH72 <ecdsa_signature> OP_PUSH33 <public_key>

            outputs_value = 0
            for txo in tx.vout:
                outputs_value += txo.nValue
            fee_paid = inputs_value - outputs_value
            assert (fee_paid > 0)

            vsize = self.getTxVSize(tx, add_bytes, add_witness_bytes)
            fee_rate_paid = fee_paid * 1000 // vsize

            self._log.info('tx amount, vsize, feerate: %ld, %ld, %ld', locked_coin, vsize, fee_rate_paid)

            if not self.compareFeeRates(fee_rate_paid, feerate):
                self._log.warning('feerate paid doesn\'t match expected: %ld, %ld', fee_rate_paid, feerate)
                # TODO: Display warning to user

        return txid, locked_n

    def verifyLockRefundTx(self, tx_bytes, lock_tx_bytes, script_out,
                           prevout_id, prevout_n, prevout_seq, prevout_script,
                           Kal, Kaf, csv_val_expect, swap_value, feerate, vkbv=None):
        # Verify:
        #   Must have only one input with correct prevout and sequence
        #   Must have only one output to the p2wsh of the lock refund script
        #   Output value must be locked_coin - lock tx fee

        tx = self.loadTx(tx_bytes)
        txid = self.getTxid(tx)
        self._log.info('Verifying lock refund tx: {}.'.format(b2h(txid)))

        ensure(tx.nVersion == self.txVersion(), 'Bad version')
        ensure(tx.nLockTime == 0, 'nLockTime not 0')
        ensure(len(tx.vin) == 1, 'tx doesn\'t have one input')

        ensure(tx.vin[0].nSequence == prevout_seq, 'Bad input nSequence')
        ensure(len(tx.vin[0].scriptSig) == 0, 'Input scriptsig not empty')
        ensure(tx.vin[0].prevout.hash == b2i(prevout_id) and tx.vin[0].prevout.n == prevout_n, 'Input prevout mismatch')

        ensure(len(tx.vout) == 1, 'tx doesn\'t have one output')

        script_pk = CScript([OP_0, hashlib.sha256(script_out).digest()])
        locked_n = findOutput(tx, script_pk)
        ensure(locked_n is not None, 'Output not found in tx')
        locked_coin = tx.vout[locked_n].nValue

        # Check script and values
        A, B, csv_val, C = self.extractScriptLockRefundScriptValues(script_out)
        ensure(A == Kal, 'Bad script pubkey')
        ensure(B == Kaf, 'Bad script pubkey')
        ensure(csv_val == csv_val_expect, 'Bad script csv value')
        ensure(C == Kaf, 'Bad script pubkey')

        fee_paid = swap_value - locked_coin
        assert (fee_paid > 0)

        dummy_witness_stack = self.getScriptLockTxDummyWitness(prevout_script)
        witness_bytes = self.getWitnessStackSerialisedLength(dummy_witness_stack)
        vsize = self.getTxVSize(tx, add_witness_bytes=witness_bytes)
        fee_rate_paid = fee_paid * 1000 // vsize

        self._log.info('tx amount, vsize, feerate: %ld, %ld, %ld', locked_coin, vsize, fee_rate_paid)

        if not self.compareFeeRates(fee_rate_paid, feerate):
            raise ValueError('Bad fee rate, expected: {}'.format(feerate))

        return txid, locked_coin, locked_n

    def verifyLockRefundSpendTx(self, tx_bytes, lock_refund_tx_bytes,
                                lock_refund_tx_id, prevout_script,
                                Kal,
                                prevout_n, prevout_value, feerate, vkbv=None):
        # Verify:
        #   Must have only one input with correct prevout (n is always 0) and sequence
        #   Must have only one output sending lock refund tx value - fee to leader's address, TODO: follower shouldn't need to verify destination addr
        tx = self.loadTx(tx_bytes)
        txid = self.getTxid(tx)
        self._log.info('Verifying lock refund spend tx: {}.'.format(b2h(txid)))

        ensure(tx.nVersion == self.txVersion(), 'Bad version')
        ensure(tx.nLockTime == 0, 'nLockTime not 0')
        ensure(len(tx.vin) == 1, 'tx doesn\'t have one input')

        ensure(tx.vin[0].nSequence == 0, 'Bad input nSequence')
        ensure(len(tx.vin[0].scriptSig) == 0, 'Input scriptsig not empty')
        ensure(tx.vin[0].prevout.hash == b2i(lock_refund_tx_id) and tx.vin[0].prevout.n == 0, 'Input prevout mismatch')

        ensure(len(tx.vout) == 1, 'tx doesn\'t have one output')

        # Destination doesn't matter to the follower
        '''
        p2wpkh = CScript([OP_0, hash160(Kal)])
        locked_n = findOutput(tx, p2wpkh)
        ensure(locked_n is not None, 'Output not found in lock refund spend tx')
        '''
        tx_value = tx.vout[0].nValue

        fee_paid = prevout_value - tx_value
        assert (fee_paid > 0)

        dummy_witness_stack = self.getScriptLockRefundSpendTxDummyWitness(prevout_script)
        witness_bytes = self.getWitnessStackSerialisedLength(dummy_witness_stack)
        vsize = self.getTxVSize(tx, add_witness_bytes=witness_bytes)
        fee_rate_paid = fee_paid * 1000 // vsize

        self._log.info('tx amount, vsize, feerate: %ld, %ld, %ld', tx_value, vsize, fee_rate_paid)

        if not self.compareFeeRates(fee_rate_paid, feerate):
            raise ValueError('Bad fee rate, expected: {}'.format(feerate))

        return True

    def verifyLockSpendTx(self, tx_bytes,
                          lock_tx_bytes, lock_tx_script,
                          a_pkhash_f, feerate, vkbv=None):
        # Verify:
        #   Must have only one input with correct prevout (n is always 0) and sequence
        #   Must have only one output with destination and amount

        tx = self.loadTx(tx_bytes)
        txid = self.getTxid(tx)
        self._log.info('Verifying lock spend tx: {}.'.format(b2h(txid)))

        ensure(tx.nVersion == self.txVersion(), 'Bad version')
        ensure(tx.nLockTime == 0, 'nLockTime not 0')
        ensure(len(tx.vin) == 1, 'tx doesn\'t have one input')

        lock_tx = self.loadTx(lock_tx_bytes)
        lock_tx_id = self.getTxid(lock_tx)

        output_script = CScript([OP_0, hashlib.sha256(lock_tx_script).digest()])
        locked_n = findOutput(lock_tx, output_script)
        ensure(locked_n is not None, 'Output not found in tx')
        locked_coin = lock_tx.vout[locked_n].nValue

        ensure(tx.vin[0].nSequence == 0, 'Bad input nSequence')
        ensure(len(tx.vin[0].scriptSig) == 0, 'Input scriptsig not empty')
        ensure(tx.vin[0].prevout.hash == b2i(lock_tx_id) and tx.vin[0].prevout.n == locked_n, 'Input prevout mismatch')

        ensure(len(tx.vout) == 1, 'tx doesn\'t have one output')
        p2wpkh = self.getScriptForPubkeyHash(a_pkhash_f)
        ensure(tx.vout[0].scriptPubKey == p2wpkh, 'Bad output destination')

        # The value of the lock tx output should already be verified, if the fee is as expected the difference will be the correct amount
        fee_paid = locked_coin - tx.vout[0].nValue
        assert (fee_paid > 0)

        dummy_witness_stack = self.getScriptLockTxDummyWitness(lock_tx_script)
        witness_bytes = self.getWitnessStackSerialisedLength(dummy_witness_stack)
        vsize = self.getTxVSize(tx, add_witness_bytes=witness_bytes)
        fee_rate_paid = fee_paid * 1000 // vsize

        self._log.info('tx amount, vsize, feerate: %ld, %ld, %ld', tx.vout[0].nValue, vsize, fee_rate_paid)

        if not self.compareFeeRates(fee_rate_paid, feerate):
            raise ValueError('Bad fee rate, expected: {}'.format(feerate))

        return True

    def signTx(self, key_bytes, tx_bytes, input_n, prevout_script, prevout_value):
        tx = self.loadTx(tx_bytes)
        sig_hash = SegwitV0SignatureHash(prevout_script, tx, input_n, SIGHASH_ALL, prevout_value)

        eck = PrivateKey(key_bytes)
        return eck.sign(sig_hash, hasher=None) + bytes((SIGHASH_ALL,))

    def signTxOtVES(self, key_sign, pubkey_encrypt, tx_bytes, input_n, prevout_script, prevout_value):
        tx = self.loadTx(tx_bytes)
        sig_hash = SegwitV0SignatureHash(prevout_script, tx, input_n, SIGHASH_ALL, prevout_value)

        return ecdsaotves_enc_sign(key_sign, pubkey_encrypt, sig_hash)

    def verifyTxOtVES(self, tx_bytes, ct, Ks, Ke, input_n, prevout_script, prevout_value):
        tx = self.loadTx(tx_bytes)
        sig_hash = SegwitV0SignatureHash(prevout_script, tx, input_n, SIGHASH_ALL, prevout_value)
        return ecdsaotves_enc_verify(Ks, Ke, sig_hash, ct)

    def decryptOtVES(self, k, esig):
        return ecdsaotves_dec_sig(k, esig) + bytes((SIGHASH_ALL,))

    def verifyTxSig(self, tx_bytes, sig, K, input_n, prevout_script, prevout_value):
        tx = self.loadTx(tx_bytes)
        sig_hash = SegwitV0SignatureHash(prevout_script, tx, input_n, SIGHASH_ALL, prevout_value)

        pubkey = PublicKey(K)
        return pubkey.verify(sig[: -1], sig_hash, hasher=None)  # Pop the hashtype byte

    def verifySig(self, pubkey, signed_hash, sig):
        pubkey = PublicKey(pubkey)
        return pubkey.verify(sig, signed_hash, hasher=None)

    def fundTx(self, tx, feerate):
        feerate_str = self.format_amount(feerate)
        # TODO: unlock unspents if bid cancelled
        options = {
            'lockUnspents': True,
            'feeRate': feerate_str,
        }
        rv = self.rpc_callback('fundrawtransaction', [tx.hex(), options])
        return bytes.fromhex(rv['hex'])

    def listInputs(self, tx_bytes):
        tx = self.loadTx(tx_bytes)

        all_locked = self.rpc_callback('listlockunspent')
        inputs = []
        for pi in tx.vin:
            txid_hex = i2h(pi.prevout.hash)
            islocked = any([txid_hex == a['txid'] and pi.prevout.n == a['vout'] for a in all_locked])
            inputs.append({'txid': txid_hex, 'vout': pi.prevout.n, 'islocked': islocked})
        return inputs

    def unlockInputs(self, tx_bytes):
        tx = self.loadTx(tx_bytes)

        inputs = []
        for pi in tx.vin:
            inputs.append({'txid': i2h(pi.prevout.hash), 'vout': pi.prevout.n})
        self.rpc_callback('lockunspent', [True, inputs])

    def signTxWithWallet(self, tx):
        rv = self.rpc_callback('signrawtransactionwithwallet', [tx.hex()])
        return bytes.fromhex(rv['hex'])

    def publishTx(self, tx):
        return self.rpc_callback('sendrawtransaction', [tx.hex()])

    def encodeTx(self, tx):
        return tx.serialize()

    def loadTx(self, tx_bytes):
        # Load tx from bytes to internal representation
        tx = CTransaction()
        tx.deserialize(BytesIO(tx_bytes))
        return tx

    def getTxid(self, tx):
        if isinstance(tx, str):
            tx = bytes.fromhex(tx)
        if isinstance(tx, bytes):
            tx = self.loadTx(tx)
        tx.rehash()
        return i2b(tx.sha256)

    def getTxOutputPos(self, tx, script):
        if isinstance(tx, bytes):
            tx = self.loadTx(tx)
        script_pk = CScript([OP_0, hashlib.sha256(script).digest()])
        return findOutput(tx, script_pk)

    def getPubkeyHash(self, K):
        return hash160(self.encodePubkey(K))

    def getScriptDest(self, script):
        return CScript([OP_0, hashlib.sha256(script).digest()])

    def getPkDest(self, K):
        return self.getScriptForPubkeyHash(self.getPubkeyHash(K))

    def scanTxOutset(self, dest):
        return self.rpc_callback('scantxoutset', ['start', ['raw({})'.format(dest.hex())]])

    def getTransaction(self, txid):
        try:
            return bytes.fromhex(self.rpc_callback('getrawtransaction', [txid.hex()]))
        except Exception as ex:
            # TODO: filter errors
            return None

    def getWalletTransaction(self, txid):
        try:
            return bytes.fromhex(self.rpc_callback('gettransaction', [txid.hex()]))
        except Exception as ex:
            # TODO: filter errors
            return None

    def setTxSignature(self, tx_bytes, stack):
        tx = self.loadTx(tx_bytes)
        tx.wit.vtxinwit.clear()
        tx.wit.vtxinwit.append(CTxInWitness())
        tx.wit.vtxinwit[0].scriptWitness.stack = stack
        return tx.serialize()

    def stripTxSignature(self, tx_bytes):
        tx = self.loadTx(tx_bytes)
        tx.wit.vtxinwit.clear()
        return tx.serialize()

    def extractLeaderSig(self, tx_bytes):
        tx = self.loadTx(tx_bytes)
        return tx.wit.vtxinwit[0].scriptWitness.stack[1]

    def extractFollowerSig(self, tx_bytes):
        tx = self.loadTx(tx_bytes)
        return tx.wit.vtxinwit[0].scriptWitness.stack[2]

    def createBLockTx(self, Kbs, output_amount):
        tx = CTransaction()
        tx.nVersion = self.txVersion()
        p2wpkh = self.getPkDest(Kbs)
        tx.vout.append(self.txoType()(output_amount, p2wpkh))
        return tx.serialize()

    def encodeSharedAddress(self, Kbv, Kbs):
        return self.pubkey_to_segwit_address(Kbs)

    def publishBLockTx(self, Kbv, Kbs, output_amount, feerate):
        b_lock_tx = self.createBLockTx(Kbs, output_amount)

        b_lock_tx = self.fundTx(b_lock_tx, feerate)
        b_lock_tx_id = self.getTxid(b_lock_tx)
        b_lock_tx = self.signTxWithWallet(b_lock_tx)

        return self.publishTx(b_lock_tx)

    def recoverEncKey(self, esig, sig, K):
        return ecdsaotves_rec_enc_key(K, esig, sig[:-1])  # Strip sighash type

    def getTxVSize(self, tx, add_bytes=0, add_witness_bytes=0):
        wsf = self.witnessScaleFactor()
        len_full = len(tx.serialize_with_witness()) + add_bytes + add_witness_bytes
        len_nwit = len(tx.serialize_without_witness()) + add_bytes
        weight = len_nwit * (wsf - 1) + len_full
        return (weight + wsf - 1) // wsf

    def findTxB(self, kbv, Kbs, cb_swap_value, cb_block_confirmed, restore_height, bid_sender):
        raw_dest = self.getPkDest(Kbs)

        rv = self.scanTxOutset(raw_dest)
        print('scanTxOutset', dumpj(rv))

        for utxo in rv['unspents']:
            if 'height' in utxo and utxo['height'] > 0 and rv['height'] - utxo['height'] > cb_block_confirmed:
                if self.make_int(utxo['amount']) != cb_swap_value:
                    self._log.warning('Found output to lock tx pubkey of incorrect value: %s', str(utxo['amount']))
                else:
                    return {'txid': utxo['txid'], 'vout': utxo['vout'], 'amount': utxo['amount'], 'height': utxo['height']}
        return None

    def waitForLockTxB(self, kbv, Kbs, cb_swap_value, cb_block_confirmed):
        raw_dest = self.getPkDest(Kbs)

        for i in range(20):
            time.sleep(1)
            rv = self.scanTxOutset(raw_dest)
            print('scanTxOutset', dumpj(rv))

            for utxo in rv['unspents']:
                if 'height' in utxo and utxo['height'] > 0 and rv['height'] - utxo['height'] > cb_block_confirmed:

                    if self.make_int(utxo['amount']) != cb_swap_value:
                        self._log.warning('Found output to lock tx pubkey of incorrect value: %s', str(utxo['amount']))
                    else:
                        return True
        return False

    def spendBLockTx(self, chain_b_lock_txid, address_to, kbv, kbs, cb_swap_value, b_fee, restore_height):
        raise ValueError('TODO')

    def getLockTxHeight(self, txid, dest_address, bid_amount, rescan_from, find_index=False):
        # Add watchonly address and rescan if required
        addr_info = self.rpc_callback('getaddressinfo', [dest_address])
        if not addr_info['iswatchonly']:
            ro = self.rpc_callback('importaddress', [dest_address, 'bid', False])
            self._log.info('Imported watch-only addr: {}'.format(dest_address))
            self._log.info('Rescanning {} chain from height: {}'.format(self.coin_name(), rescan_from))
            self.rpc_callback('rescanblockchain', [rescan_from])

        return_txid = True if txid is None else False
        if txid is None:
            txns = self.rpc_callback('listunspent', [0, 9999999, [dest_address, ]])

            for tx in txns:
                if self.make_int(tx['amount']) == bid_amount:
                    txid = bytes.fromhex(tx['txid'])
                    break

        if txid is None:
            return None

        try:
            tx = self.rpc_callback('gettransaction', [txid.hex()])

            block_height = 0
            if 'blockhash' in tx:
                block_header = self.rpc_callback('getblockheader', [tx['blockhash']])
                block_height = block_header['height']

            rv = {
                'depth': 0 if 'confirmations' not in tx else tx['confirmations'],
                'height': block_height}

        except Exception as e:
            self._log.debug('getLockTxHeight gettransaction failed: %s, %s', txid.hex(), str(e))
            return None

        if find_index:
            tx_obj = self.rpc_callback('decoderawtransaction', [tx['hex']])
            rv['index'] = find_vout_for_address_from_txobj(tx_obj, dest_address)

        if return_txid:
            rv['txid'] = txid.hex()

        return rv

    def getOutput(self, txid, dest_script, expect_value, xmr_swap=None):
        # TODO: Use getrawtransaction if txindex is active
        utxos = self.rpc_callback('scantxoutset', ['start', ['raw({})'.format(dest_script.hex())]])
        if 'height' in utxos:  # chain_height not returned by v18 codebase
            chain_height = utxos['height']
        else:
            chain_height = self.getChainHeight()
        rv = []
        for utxo in utxos['unspents']:
            if txid and txid.hex() != utxo['txid']:
                continue

            if expect_value != self.make_int(utxo['amount']):
                continue

            rv.append({
                'depth': 0 if 'height' not in utxo else (chain_height - utxo['height']) + 1,
                'height': 0 if 'height' not in utxo else utxo['height'],
                'amount': self.make_int(utxo['amount']),
                'txid': utxo['txid'],
                'vout': utxo['vout']})
        return rv, chain_height

    def withdrawCoin(self, value, addr_to, subfee):
        params = [addr_to, value, '', '', subfee, True, self._conf_target]
        return self.rpc_callback('sendtoaddress', params)

    def signCompact(self, k, message):
        message_hash = hashlib.sha256(bytes(message, 'utf-8')).digest()

        privkey = PrivateKey(k)
        return privkey.sign_recoverable(message_hash, hasher=None)[:64]

    def verifyCompact(self, K, message, sig):
        message_hash = hashlib.sha256(bytes(message, 'utf-8')).digest()
        pubkey = PublicKey(K)
        rv = pubkey.verify_compact(sig, message_hash, hasher=None)
        assert (rv is True)

    def verifyMessage(self, address, message, signature, message_magic=None) -> bool:
        if message_magic is None:
            message_magic = self.chainparams_network()['message_magic']

        message_bytes = SerialiseNumCompact(len(message_magic)) + bytes(message_magic, 'utf-8') + SerialiseNumCompact(len(message)) + bytes(message, 'utf-8')
        message_hash = hashlib.sha256(hashlib.sha256(message_bytes).digest()).digest()
        signature_bytes = base64.b64decode(signature)
        rec_id = (signature_bytes[0] - 27) & 3
        signature_bytes = signature_bytes[1:] + bytes((rec_id,))
        try:
            pubkey = PublicKey.from_signature_and_message(signature_bytes, message_hash, hasher=None)
        except Exception as e:
            self._log.info('verifyMessage failed: ' + str(e))
            return False

        address_hash = self.decodeAddress(address)
        pubkey_hash = hash160(pubkey.format())

        return True if address_hash == pubkey_hash else False

    def showLockTransfers(self, Kbv, Kbs):
        raise ValueError('Unimplemented')

    def getLockTxSwapOutputValue(self, bid, xmr_swap):
        return bid.amount

    def getLockRefundTxSwapOutputValue(self, bid, xmr_swap):
        return xmr_swap.a_swap_refund_value

    def getLockRefundTxSwapOutput(self, xmr_swap):
        # Only one prevout exists
        return 0

    def getScriptLockTxDummyWitness(self, script):
        return [
            b''.hex(),
            bytes(72).hex(),
            bytes(72).hex(),
            bytes(len(script)).hex()
        ]

    def getScriptLockRefundSpendTxDummyWitness(self, script):
        return [
            b''.hex(),
            bytes(72).hex(),
            bytes(72).hex(),
            bytes((1,)).hex(),
            bytes(len(script)).hex()
        ]

    def getScriptLockRefundSwipeTxDummyWitness(self, script):
        return [
            bytes(72).hex(),
            b''.hex(),
            bytes(len(script)).hex()
        ]

    def getWitnessStackSerialisedLength(self, witness_stack):
        length = getCompactSizeLen(len(witness_stack))
        for e in witness_stack:
            length += getWitnessElementLen(len(e) // 2)  # hex -> bytes

        # See core SerializeTransaction
        length += 32 + 4 + 1 + 4  # vinDummy
        length += 1  # flags
        return length

    def describeTx(self, tx_hex):
        return self.rpc_callback('decoderawtransaction', [tx_hex])

    def getSpendableBalance(self):
        return self.make_int(self.rpc_callback('getbalances')['mine']['trusted'])

    def createUTXO(self, value_sats):
        # Create a new address and send value_sats to it

        spendable_balance = self.getSpendableBalance()
        if spendable_balance < value_sats:
            raise ValueError('Balance too low')

        address = self.getNewAddress(self._use_segwit, 'create_utxo')
        return self.withdrawCoin(self.format_amount(value_sats), address, False), address

    def createRawSignedTransaction(self, addr_to, amount):
        txn = self.rpc_callback('createrawtransaction', [[], {addr_to: self.format_amount(amount)}])

        options = {
            'lockUnspents': True,
            'conf_target': self._conf_target,
        }
        txn_funded = self.rpc_callback('fundrawtransaction', [txn, options])['hex']
        txn_signed = self.rpc_callback('signrawtransactionwithwallet', [txn_funded])['hex']
        return txn_signed

    def getBlockWithTxns(self, block_hash):
        return self.rpc_callback('getblock', [block_hash, 2])


def testBTCInterface():
    print('testBTCInterface')


if __name__ == "__main__":
    testBTCInterface()
