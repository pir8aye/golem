import atexit
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from distutils.version import StrictVersion

import requests
from ethereum.keys import privtoaddr
from ethereum.transactions import Transaction
from ethereum.utils import normalize_address, denoms
from web3 import Web3, IPCProvider, HTTPProvider

from golem.core.common import is_windows, DEVNULL
from golem.environments.utils import find_program
from golem.report import report_calls, Component
from golem.utils import encode_hex, decode_hex
from golem.utils import find_free_net_port
from golem.utils import tee_target
from golem_messages.cryptography import privtopub

log = logging.getLogger('golem.ethereum')


NODE_LIST_URL = 'https://rinkeby.golem.network'
FALLBACK_NODE_LIST = [
    'http://188.165.227.180:55555',
    'http://94.23.17.170:55555',
    'http://94.23.57.58:55555',
]
DONATE_URL_TEMPLATE = "http://188.165.227.180:4000/donate/{}"


def get_public_nodes():
    """Returns public geth RPC addresses"""
    try:
        return requests.get(NODE_LIST_URL).json()
    except Exception as exc:
        log.error("Error downloading node list: %s", exc)

    addr_list = FALLBACK_NODE_LIST[:]
    random.shuffle(addr_list)
    return addr_list


def tETH_faucet_donate(addr):
    addr = normalize_address(addr)
    request = DONATE_URL_TEMPLATE.format(addr.hex())
    response = requests.get(request)
    if response.status_code != 200:
        log.error("tETH Faucet error code {}".format(response.status_code))
        return False
    response = response.json()
    if response['paydate'] == 0:
        log.warning("tETH Faucet warning {}".format(response['message']))
        return False
    # The paydate is not actually very reliable, usually some day in the past.
    paydate = datetime.fromtimestamp(response['paydate'])
    amount = int(response['amount']) / denoms.ether
    log.info("Faucet: {:.6f} ETH on {}".format(amount, paydate))
    return True


class Faucet(object):
    PRIVKEY = "{:32}".format("Golem Faucet").encode()
    PUBKEY = privtopub(PRIVKEY)
    ADDR = privtoaddr(PRIVKEY)

    @staticmethod
    def gimme_money(ethnode, addr, value):
        nonce = ethnode.get_transaction_count(encode_hex(Faucet.ADDR))
        addr = normalize_address(addr)
        tx = Transaction(nonce, 1, 21000, addr, value, '')
        tx.sign(Faucet.PRIVKEY)
        h = ethnode.send(tx)
        log.info("Faucet --({} ETH)--> {} ({})".format(value / denoms.ether,
                                                       encode_hex(addr), h))
        h = decode_hex(h[2:])
        return h


class NodeProcess(object):

    MIN_GETH_VERSION = StrictVersion('1.7.2')
    MAX_GETH_VERSION = StrictVersion('1.7.999')
    CONNECTION_TIMEOUT = 10
    CHAIN = 'rinkeby'

    SUBPROCESS_PIPES = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=DEVNULL
    )

    def __init__(self, datadir, addr=None, start_node=False):
        """
        :param datadir: working directory
        :param addr: address of a geth instance to connect with
        :param start_node: start a new geth node
        """
        self.datadir = datadir
        self.start_node = start_node
        self.web3 = None  # web3 client interface
        self.addr_list = [addr] if addr else get_public_nodes()

        self.__prog = None  # geth location
        self.__ps = None  # child process

    def is_running(self):
        return self.__ps is not None

    @report_calls(Component.ethereum, 'node.start')
    def start(self, start_port=None):
        if self.__ps is not None:
            raise RuntimeError("Ethereum node already started by us")

        if self.start_node:
            provider = self._create_local_ipc_provider(self.CHAIN, start_port)
        else:
            provider = self._create_remote_rpc_provider()

        self.web3 = Web3(provider)
        atexit.register(lambda: self.stop())

        started = time.time()
        deadline = started + self.CONNECTION_TIMEOUT

        while not self.is_connected():
            if time.time() > deadline:
                return self._start_timed_out(provider, start_port)
            time.sleep(0.1)

        genesis_block = self.get_genesis_block()

        while not genesis_block:
            if time.time() > deadline:
                return self._start_timed_out(provider, start_port)
            time.sleep(0.5)
            genesis_block = self.get_genesis_block()

        identified_chain = self.identify_chain(genesis_block)
        if identified_chain != self.CHAIN:
            raise OSError("Wrong '{}' Ethereum chain".format(identified_chain))

        log.info("Connected to node in %ss", time.time() - started)

        return None

    @report_calls(Component.ethereum, 'node.stop')
    def stop(self):
        if self.__ps:
            start_time = time.clock()

            try:
                self.__ps.terminate()
                self.__ps.wait()
            except subprocess.NoSuchProcess:
                log.warn("Cannot terminate node: process {} no longer exists"
                         .format(self.__ps.pid))

            self.__ps = None
            duration = time.clock() - start_time
            log.info("Node terminated in {:.2f} s".format(duration))

    def is_connected(self):
        try:
            return self.web3.isConnected()
        except AssertionError:  # thrown if not all required APIs are available
            return False

    def identify_chain(self, genesis_block):
        """Check what chain the Ethereum node is running."""
        GENESES = {
        '0xd4e56740f876aef8c010b86a40d5f56745a118d0906a34e69aec8c0db1cb8fa3':
            'mainnet',  # noqa
        '0x41941023680923e0fe4d74a34bdac8141f2540e3ae90623718e47d66d1ca4a2d':
            'ropsten',  # noqa
        '0x6341fd3daf94b748c72ced5a5b26028f2474f5f00d824504e4fa37a75767e177':
            'rinkeby',  # noqa
        }
        genesis = genesis_block['hash']
        chain = GENESES.get(genesis, 'unknown')
        log.info("{} chain ({})".format(chain, genesis))
        return chain

    def get_genesis_block(self):
        try:
            return self.web3.eth.getBlock(0)
        except Exception:  # pylint:disable=broad-except
            return None

    def _start_timed_out(self, provider, start_port):
        if not self.start_node:
            self.start_node = not self.addr_list
            return self.start(start_port)
        raise OSError("Cannot connect to geth: {}".format(provider))

    def _create_local_ipc_provider(self, chain, start_port=None):  # noqa pylint: disable=too-many-locals
        self._find_geth()

        # Init geth datadir
        geth_log_dir = os.path.join(self.datadir, "logs")
        geth_log_path = os.path.join(geth_log_dir, "geth.log")
        geth_datadir = os.path.join(self.datadir, 'ethereum', chain)

        os.makedirs(geth_log_dir, exist_ok=True)

        if start_port is None:
            start_port = find_free_net_port()

        # Build unique IPC/socket path. We have to use system temp dir to
        # make sure the path has length shorter that ~100 chars.
        tempdir = tempfile.gettempdir()
        ipc_file = '{}-{}'.format(chain, start_port)
        ipc_path = os.path.join(tempdir, ipc_file)

        if is_windows():
            # On Windows expand to full named pipe path.
            ipc_path = r'\\.\pipe\{}'.format(self.start_node)

        args = [
            self.__prog,
            '--datadir={}'.format(geth_datadir),
            '--cache=32',
            '--syncmode=light',
            '--rinkeby',
            '--port={}'.format(start_port),
            '--ipcpath={}'.format(ipc_path),
            '--nousb',
            '--verbosity', '3',
        ]

        log.info("Starting Ethereum node: `{}`".format(" ".join(args)))
        self.__ps = subprocess.Popen(args, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     stdin=DEVNULL)

        tee_kwargs = {
            'proc': self.__ps,
            'path': geth_log_path,
        }
        channels = (
            ('GETH', self.__ps.stderr, sys.stderr),
            ('GETHO', self.__ps.stdout, sys.stdout),
        )
        for prefix, in_, out in channels:
            tee_kwargs['prefix'] = prefix + ': '
            tee_kwargs['input_stream'] = in_
            tee_kwargs['stream'] = out
            thread_name = 'tee-' + prefix
            tee_thread = threading.Thread(name=thread_name, target=tee_target,
                                          kwargs=tee_kwargs)
            tee_thread.start()

        return IPCProvider(ipc_path)

    def _create_remote_rpc_provider(self):
        addr = self.addr_list.pop()
        log.info('GETH: connecting to remote RPC interface at %s', addr)
        return HTTPProvider(addr)

    def _find_geth(self):
        geth = find_program('geth')
        if not geth:
            raise OSError("Ethereum client 'geth' not found")

        output, _ = subprocess.Popen(
            [geth, 'version'],
            **self.SUBPROCESS_PIPES
        ).communicate()

        match = re.search("Version: (\d+\.\d+\.\d+)",
                          str(output, 'utf-8')).group(1)

        ver = StrictVersion(match)
        if ver < self.MIN_GETH_VERSION or ver > self.MAX_GETH_VERSION:
            raise OSError("Incompatible geth version: {}. "
                          "Expected >= {} and <= {}"
                          .format(ver, self.MIN_GETH_VERSION,
                                  self.MAX_GETH_VERSION))

        log.info("geth {}: {}".format(ver, geth))
        self.__prog = geth
