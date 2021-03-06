 #!/usr/bin/env python
# encoding=utf8

__author__ = 'sunnypickle'

######################################################################
#  数据处理逻辑：
#  1. 先从数据库中获取出上次采集已成功提交的区块号
#  2. 采集前清理掉超出此区块号的tbl_block, tbl_transaction, tbl_transaction_ex, tbl_contract_info表中相关记录
#  3. 考虑到对同一个合约的操作，可能会有并发问题导致的合约操作的先后顺序颠倒的问题，
#       对于tbl_contract_info表，采用replace into ON DUPLICATE的方式
#  4. 对于tbl_contract_abi, tbl_contract_storage, tbl_contract_event表，在遇到注册相关合约相关的交易的处理上，
#       先清理，再插入
######################################################################



import logging
import sys
import traceback
import leveldb
import time
import threading
import pybitcointools
import json
from block_usdt import BlockInfoBtc
from datetime import datetime
from coin_tx_collector import CoinTxCollector
from collector_conf import USDTCollectorConfig
from wallet_api import WalletApi
from Queue import Queue
from gevent import Greenlet
import gevent


gLock = threading.Lock()
q = Queue()


class CacheManager(object):
    def __init__(self, sync_key, symbol):
        self.sync_key = sync_key
        self.multisig_address_cache = set()
        self.block_cache = []
        self.withdraw_transaction_cache = []
        self.deposit_transaction_cache = []
        self.flush_thread = None
        self.symbol = symbol

    def flush_to_db(self, db):
        blocks = self.block_cache
        withdraw_transaction = self.withdraw_transaction_cache
        deposit_transaction = self.deposit_transaction_cache
        if self.flush_thread is not None:
            self.flush_thread.join()
            self.flush_thread = None
        self.flush_thread = threading.Thread(target=CacheManager.flush_process,
                                        args=(self.symbol,db, [
                                                  db.b_block,
                                                  db.b_deposit_transaction,
                                                  db.b_withdraw_transaction
                                              ],
                                              [
                                                  blocks,
                                                  deposit_transaction,
                                                  withdraw_transaction
                                              ],
                                              self.sync_key))
        self.flush_thread.start()
        self.block_cache = []
        self.withdraw_transaction_cache = []
        self.deposit_transaction_cache = []


    @staticmethod
    def flush_process(symbol,db, tables, data, sync_key):
        for i, t in enumerate(tables):
            if len(data[i]) > 0:
                logging.debug(data[i][0])
                t.insert(data[i])
        block_num = data[0][len(data[0])-1]["blockNumber"]
        #logging.info(sync_key + ": " + str(block_num))

       #Update sync block number finally.

        db.b_config.update({"key": sync_key}, {
            "$set": {"key": sync_key, "value": str(block_num)}})


class CollectBlockThread(threading.Thread):
    # self.config.ASSET_SYMBOL.lower()
    def __init__(self, db, config, wallet_api,sync_status):
        threading.Thread.__init__(self)
        self.stop_flag = False
        self.db = db
        self.config = config
        self.wallet_api = wallet_api
        self.last_sync_block_num = 0
        self.sync_status = sync_status


    def run(self):
        # 清理上一轮的垃圾数据，包括块数据、交易数据以及合约数据
        self.last_sync_block_num = self.clear_last_garbage_data()
        self.process_blocks()
    def get_sync_status(self):
        return self.sync_status
    def stop(self):
        self.stop_flag = True

    def collect_block_and_cache(self, last_sync_block_num,datas):
        block_info = self.collect_block( last_sync_block_num)
        datas[last_sync_block_num] = block_info

    def clear_last_garbage_data(self):
        ret = self.db.b_config.find_one({"key": self.config.SYNC_BLOCK_NUM})
        if ret is None:
            return 0
        last_sync_block_num = int(ret["value"])
        try:
            self.db.b_raw_transaction.remove(
                {"blockNum": {"$gte": last_sync_block_num}, "chainId": self.config.ASSET_SYMBOL.lower()})
            self.db.b_block.remove(
                {"blockNumber": {"$gte": last_sync_block_num}, "chainId": self.config.ASSET_SYMBOL.lower()})
            self.db.b_raw_transaction_input.remove(
                {"blockNum": {"$gte": last_sync_block_num}, "chainId": self.config.ASSET_SYMBOL.lower()})
            self.db.b_raw_transaction_output.remove(
                {"blockNum": {"$gte": last_sync_block_num}, "chainId": self.config.ASSET_SYMBOL.lower()})
            self.db.b_deposit_transaction.remove(
                {"blockNum": {"$gte": last_sync_block_num}, "chainId": self.config.ASSET_SYMBOL.lower()})
            self.db.b_withdraw_transaction.remove(
                {"blockNum": {"$gte": last_sync_block_num}, "chainId": self.config.ASSET_SYMBOL.lower()})
        except Exception, ex:
            print ex
        return int(last_sync_block_num)


    def process_blocks(self):
        # 线程启动，设置为同步状态
        config_db = self.db.b_config
        ret = config_db.find_one({"key": self.config.SYNC_STATE_FIELD})
        if ret is None:
            config_db.insert({"key": self.config.SYNC_STATE_FIELD, "value": "false"})
            config_db.insert({"key": self.config.SYNC_BLOCK_NUM, "value": "0"})
            config_db.insert({"key": self.config.SAFE_BLOCK_FIELD, "value": 6})
        config_db.update({"key": self.config.SYNC_STATE_FIELD},
                         {"key": self.config.SYNC_STATE_FIELD, "value": "true"})

        while self.stop_flag is False :
            self.latest_block_num = self._get_latest_block_num()
            if self.latest_block_num is None or self.last_sync_block_num >= self.latest_block_num :
                self.sync_status = False
                if self.latest_block_num is None:
                    print "waiting(10s) collector1 http rpc"
                    time.sleep(10)
                else:
                    time.sleep(10)
                continue
            try:
                # 获取当前链上最新块号
                logging.debug("latest_block_num: %d, last_sync_block_num: %d" %
                              (self.latest_block_num, self.last_sync_block_num))
                if q.qsize() > 10:
                    logging.info(q.qsize())
                    time.sleep(1)
                    continue
                # Collect single block info
                collect_thread_count = 1
                if self.latest_block_num -self.last_sync_block_num > self.config.COLLECT_THREAD:
                    collect_thread_count = self.config.COLLECT_THREAD
                thread_id_list =[]
                datas ={}
                start_id = self.last_sync_block_num
                for i in range(collect_thread_count):
                    thread_id = Greenlet.spawn(self.collect_block_and_cache, self.last_sync_block_num,datas)
                    thread_id_list.append(thread_id)
                    self.last_sync_block_num += 1
                    if self.last_sync_block_num % 10000 == 0:
                        self._show_progress(self.last_sync_block_num, self.latest_block_num)
                gevent.joinall(thread_id_list)
                if len(datas) != collect_thread_count:
                    logging.error("collect_count is error: " + len(datas) + ":" + collect_thread_count)
                    self.last_sync_block_num = self.last_sync_block_num - collect_thread_count
                    self.clear_last_garbage_data()
                else:
                    for i in range(collect_thread_count):
                        q.put(datas[start_id+i])
            except Exception, ex:
                logging.info(traceback.format_exc())
                print ex
                # 异常情况，60秒后重试
                time.sleep(60)
                self.process_blocks()


    #采集块数据
    def collect_block(self, block_num_fetch):
        ret1 = self.wallet_api.http_request("omni_listblocktransactions", [block_num_fetch])
        if ret1['result'] == None:
            raise Exception("omni_listblocktransactions error")
        block_info = BlockInfoBtc()
        block_info.from_onmi_list_transaction(block_num_fetch,ret1['result'])
        logging.debug("Collect block [num:%d], [tx_num:%d]" % (block_num_fetch, len(ret1['result'])))
        return block_info


    @staticmethod
    def _show_progress(current_block, total_block):
        sync_rate = float(current_block) / total_block
        sync_process = '#' * int(40 * sync_rate) + ' ' * (40 - int(40 * sync_rate))
        sys.stdout.write("\rtime: %s  sync block [%s][%d/%d], %.3f%%\n" % (time.strftime("%y-%m-%d %H:%M:%S",time.localtime())
                                                                             ,sync_process, current_block, total_block, sync_rate * 100))


    def _get_latest_block_num(self):
        ret = self.wallet_api.http_request("getblockcount", [])
        if ret.has_key("result"):
            real_block_num = ret["result"]
            if real_block_num is None:
                return None
            safe_block = 6
            safe_block_ret = self.db.b_config.find_one({"key": self.config.SAFE_BLOCK_FIELD})
            if safe_block_ret is not None:
                safe_block = int(safe_block_ret["value"])
            return int(real_block_num) - safe_block
        else:
            return None


class USDTCoinTxCollector(CoinTxCollector):
    sync_status = True

    def __init__(self, db):
        super(USDTCoinTxCollector, self).__init__()

        self.stop_flag = False
        self.db = db
        self.t_multisig_address = self.db.b_usdt_multisig_address
        self.multisig_address_cache = set()
        self.config = USDTCollectorConfig()
        conf = {"host": self.config.RPC_HOST, "port": self.config.RPC_PORT,"rpc_user":self.config.RPC_USER,"rpc_password":self.config.RPC_PASSWORD}
        self.wallet_api = WalletApi(self.config.ASSET_SYMBOL, conf)
        self.cache = CacheManager(self.config.SYNC_BLOCK_NUM, self.config.ASSET_SYMBOL)


    def _update_cache(self):
        for addr in self.t_multisig_address.find({"addr_type": 0}):
            self.multisig_address_cache.add(addr["address"])


    def do_collect_app(self):
        self._update_cache()
        self.collect_thread = CollectBlockThread(self.db, self.config, self.wallet_api,self.sync_status)
        self.collect_thread.start()
        count = 0
        last_block = 0
        while self.stop_flag is False:

            count += 1
            block = q.get()
            if last_block >= block.block_num:
                logging.error("Unordered block number: " + str(last_block) + ":" + str(block.block_num))
            last_block = block.block_num
            # Update block table
            # t_block = self.db.b_block
            logging.debug("Block number: " + str(block.block_num) + ", Transaction number: " + str(len(block.transactions )))
            self.cache.block_cache.append(block.get_json_data())
            # Process each transaction
            #print block.block_num,block.transactions
            for trx_id in block.transactions:
                logging.debug("Transaction: %s" % trx_id)
                if self.config.ASSET_SYMBOL == "USDT":
                    if block.block_num == 0:
                        continue
                    trx_data = self.get_transaction_data(trx_id)
                pretty_trx_info = self.collect_pretty_transaction(self.db, trx_data, block.block_num)
            self.sync_status = self.collect_thread.get_sync_status()
            if  self.sync_status:
                logging.debug(str(count) + " blocks processed, flush to db")
                self.cache.flush_to_db(self.db)
            elif self.sync_status is False :
                self.cache.flush_to_db(self.db)
                self._update_cache()
                time.sleep(2)

        self.collect_thread.stop()
        self.collect_thread.join()


    def get_transaction_data(self, trx_id):
        ret = self.wallet_api.http_request("omni_gettransaction", [trx_id])
        if ret["result"] is None:
            resp_data = None
        else:
            resp_data = ret["result"]
        return resp_data


    def collect_pretty_transaction(self, db_pool, base_trx_data, block_num):
        trx_data = {}
        trx_data["chainId"] = self.config.ASSET_SYMBOL.lower()
        trx_data["trxid"] = base_trx_data["txid"]
        trx_data["blockNum"] = block_num
        target_property_id = self.config.PROPERTYID

        print trx_data["trxid"]
        if not base_trx_data.has_key("type_int"):
            print "not has type",base_trx_data
            return trx_data
        trx_type = base_trx_data["type_int"]

        if trx_type ==0:
            print base_trx_data
            value = float(base_trx_data["amount"])
            if not base_trx_data.has_key("propertyid"):
                print "is create omni transaction:", base_trx_data
                return trx_data
            propertyId = base_trx_data["propertyid"]
        elif trx_type ==4:
            if not base_trx_data.has_key("ecosystem"):
                print "not has ecosystem", base_trx_data
                return trx_data
            ecoSystem = base_trx_data["ecosystem"]
            if ecoSystem != "main":
                print "isn't main net transaction"
                return trx_data
            print base_trx_data
            propertyId =-1
            if not base_trx_data.has_key("subsends"):
                print "not has subsends", base_trx_data
                return trx_data
            for data in base_trx_data["subsends"]:
                if data["propertyid"] == target_property_id:
                    propertyId = data["propertyid"]
                    value = float(data["amount"])
        else:
            is_valid_tx = False
            return trx_data



        if propertyId !=target_property_id:
            is_valid_tx =False
            return trx_data


        from_address = base_trx_data["sendingaddress"]
        to_address = base_trx_data["referenceaddress"]
        is_valid_tx = base_trx_data["valid"]

        fee = float(base_trx_data["fee"])

        out_set = {}
        in_set = {}
        multisig_in_addr = ""
        multisig_out_addr = ""
        out_set[to_address]=value
        in_set[from_address]=value



        """
        Only 3 types of transactions will be filtered out and be record in database.
        1. deposit transaction (from_address only one no LINK address and to_address only one LINK address)
        2. withdraw transaction (from_address only one LINK address and to_address no other LINK address)
        3. transaction between hot-wallet and cold-wallet (from_address contains only one LINK address and to_address contains only one other LINK address)

        Check logic:
        1. check all tx in vin and store addresses & values (if more than one LINK address set invalid)
        2. check all tx in vout and store all non-change addresses & values (if more than one LINK address set invalid)
        3. above logic filter out the situation - more than one LINK address in vin or vout but there is one condition
           should be filter out - more than one normal address in vin for deposit transaction
        4. then we can record the transaction according to transaction type
           only one other addres in vin and only one LINK address in vout - deposit
           only one LINK addres in vin and only other addresses in vout - withdraw
           only one LINK addres in vin and only one other LINK address in vout - transaction between hot-wallet and cold-wallet
           no LINK address in vin and no LINK address in vout - transaction that we don't care about, record nothing
        5. record original transaction in raw table if we care about it.
        """
        if is_valid_tx:
            print "from_addrsss:",from_address,"to_address",to_address,"value",value,"type",trx_type,"propertyId",propertyId,"fee",fee
        if from_address in  self.multisig_address_cache:
            if multisig_in_addr == "":
                multisig_in_addr = from_address

        if to_address in self.multisig_address_cache:
            if multisig_out_addr == "":
                multisig_out_addr = to_address

        if not multisig_in_addr == "" and not multisig_out_addr == "": # maybe transfer between hot-wallet and cold-wallet
            if not is_valid_tx:
                logging.error("Invalid transaction between hot-wallet and cold-wallet")
                trx_data['type'] = -3
            else:
                trx_data['type'] = 0
        elif not multisig_in_addr == "": # maybe withdraw
            if not is_valid_tx:
                logging.error("Invalid withdraw transaction")
                trx_data['type'] = -1
            else:
                trx_data['type'] = 1
        elif not multisig_out_addr == "": # maybe deposit
            if not is_valid_tx or not len(in_set) == 1:
                logging.error("Invalid deposit transaction")
                trx_data['type'] = -2
            else:
                trx_data['type'] = 2
        else:
            logging.debug("Nothing to record")
            return

        #trx_data["trxTime"] = datetime.utcfromtimestamp(base_trx_data['time']).strftime("%Y-%m-%d %H:%M:%S")
        #trx_data["createtime"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if trx_data['type'] == 2 or trx_data['type'] == 0:
            deposit_data = {
                "txid": base_trx_data["txid"],
                "from_account": from_address,
                "to_account": multisig_out_addr,
                "amount": "%.8f"%value,
                "fee":"%.8f"%fee,
                "asset_symbol": self.config.ASSET_SYMBOL,
                "blockNum": block_num,
                "chainId": self.config.ASSET_SYMBOL.lower()
            }
            self.cache.deposit_transaction_cache.append(deposit_data)
        elif trx_data['type'] == 1:
            for k, v in out_set.items():
                withdraw_data = {
                    "txid": base_trx_data["txid"],
                    "from_account": multisig_in_addr,
                    "to_account": k,
                    "amount": "%.8f"%value,
                    "fee": "%.8f"%fee,
                    "asset_symbol": self.config.ASSET_SYMBOL,
                    "blockNum": block_num,
                    "chainId": self.config.ASSET_SYMBOL.lower()
                }
                self.cache.withdraw_transaction_cache.append(withdraw_data)

        # logging.info("add raw transaction")
        #self.cache.raw_transaction_cache.append(trx_data)
        return trx_data

