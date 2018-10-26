import argparse
import json
import os
from threading import Thread
import time
import sys

from .client import RpcProxyClient
from .differentials import DifferentialTester
from .echidna import echidna_exists, EchidnaPlugin, install_echidna
from .etheno import app, EthenoView, GETH_DEFAULT_RPC_PORT, ManticoreClient, ETHENO
from .genesis import Account, make_accounts, make_genesis
from .synchronization import AddressSynchronizingClient, RawTransactionClient
from .utils import clear_directory, decode_value, find_open_port, format_hex_address, ynprompt
from . import Etheno
from . import ganache
from . import geth
from . import logger
from . import manticoreutils
from . import parity
from . import truffle

def main(argv = None):
    parser = argparse.ArgumentParser(description='An Ethereum JSON RPC multiplexer and Manticore wrapper')
    parser.add_argument('--debug', action='store_true', default=False, help='Enable debugging from within the web server')
    parser.add_argument('--run-publicly', action='store_true', default=False, help='Allow the web server to accept external connections')
    parser.add_argument('-p', '--port', type=int, default=GETH_DEFAULT_RPC_PORT, help='Port on which to run the JSON RPC webserver (default=%d)' % GETH_DEFAULT_RPC_PORT)
    parser.add_argument('-a', '--accounts', type=int, default=None, help='Number of accounts to create in the client (default=10)')
    parser.add_argument('-b', '--balance', type=float, default=100.0, help='Default balance (in Ether) to seed to each account (default=100.0)')
    parser.add_argument('-c', '--gas-price', type=int, default=None, help='Default gas price (default=20000000000)')
    parser.add_argument('-i', '--network-id', type=int, default=None, help='Specify a network ID (default is the network ID of the master client)')
    parser.add_argument('-m', '--manticore', action='store_true', default=False, help='Run all transactions through manticore')
    parser.add_argument('-r', '--manticore-script', type=argparse.FileType('rb'), default=None, help='Instead of running automated detectors and analyses, run this Manticore script')
    parser.add_argument('--manticore-max-depth', type=int, default=None, help='Maximum state depth for Manticore to explore')
    parser.add_argument('-e', '--echidna', action='store_true', default=False, help='Fuzz the clients using transactions generated by Echidna')
    parser.add_argument('--fuzz-limit', type=int, default=None, help='The maximum number of transactions for Echidna to generate (default=unlimited)')
    parser.add_argument('-t', '--truffle', action='store_true', default=False, help='Run the truffle migrations in the current directory and exit')
    parser.add_argument('--truffle-args', type=str, default='migrate', help='Arguments to pass to truffle (default=migrate)')
    parser.add_argument('-g', '--ganache', action='store_true', default=False, help='Run Ganache as a master JSON RPC client (cannot be used in conjunction with --master)')
    parser.add_argument('--ganache-args', type=str, default=None, help='Additional arguments to pass to Ganache')
    parser.add_argument('--ganache-port', type=int, default=None, help='Port on which to run Ganache (defaults to the closest available port to the port specified with --port plus one)')
    parser.add_argument('-go', '--geth', action='store_true', default=False, help='Run Geth as a JSON RPC client')
    parser.add_argument('--geth-port', type=int, default=None, help='Port on which to run Geth (defaults to the closest available port to the port specified with --port plus one)')
    parser.add_argument('-pa', '--parity', action='store_true', default=False, help='Run Parity as a JSON RPC client')
    parser.add_argument('--parity-port', type=int, default=None, help='Port on which to run Parity (defaults to the closest available port to the port specified with --port plus one)')
    parser.add_argument('-j', '--genesis', type=str, default=None, help='Path to a genesis.json file to use for initializing clients. Any genesis-related options like --network-id will override the values in this file. If --accounts is greater than zero, that many new accounts will be appended to the accounts in the genesis file.')
    parser.add_argument('--save-genesis', type=str, default=None, help="Save a genesis.json file to reproduce the state of this run. Note that this genesis file will include all known private keys for the genesis accounts, so use this with caution.")
    parser.add_argument('--no-differential-testing', action='store_false', dest='run_differential', default=True, help='Do not run differential testing, which is run by default')
    parser.add_argument('-l', '--log-level', type=str.upper, choices={'CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'}, default='INFO', help='Set Etheno\'s log level (default=INFO)')
    parser.add_argument('--log-file', type=str, default=None, help='Path to save all log output to a single file')
    parser.add_argument('--log-dir', type=str, default=None, help='Path to a directory in which to save all log output, divided by logging source')
    parser.add_argument('-v', '--version', action='store_true', default=False, help='Print version information and exit')
    parser.add_argument('client', type=str, nargs='*', help='JSON RPC client URLs to multiplex; if no client is specified for --master, the first client in this list will default to the master (format="http://foo.com:8545/")')
    parser.add_argument('-s', '--master', type=str, default=None, help='A JSON RPC client to use as the master (format="http://foo.com:8545/")')
    parser.add_argument('--raw', type=str, nargs='*', help='JSON RPC client URLs to multiplex that do not have any local accounts; Etheno will automatically use auto-generated accounts with known private keys, pre-sign all transactions, and only use eth_sendRawTransaction')

    if argv is None:
        argv = sys.argv
    
    args = parser.parse_args(argv[1:])

    if args.version:
        print(VERSION_NAME)
        sys.exit(0)

    ETHENO.log_level = args.log_level

    if args.log_file:
        ETHENO.logger.save_to_file(args.log_file)

    if args.log_dir:
        if os.path.exists(args.log_dir):
            if not ynprompt("Logging path `%s` already exists! Would you like to overwrite it? [yN] " % args.log_dir):
                sys.exit(1)
            elif os.path.isfile(args.log_dir):
                os.remove(args.log_dir)
            else:
                # don't delete the directory, just its contents
                # we can't use shutil.rmtree here, because that deletes the directory and also it doesn't work on symlinks
                if not ynprompt("We are about to delete the contents of `%s`. Are you sure? [yN] " % args.log_dir):
                    sys.exit(1)
                abspath = os.path.abspath(args.log_dir)
                if abspath == '' or abspath == '/' or abspath.endswith('://') or abspath.endswith(':\\\\'):
                    print("Wait a sec, you want me to delete `%s`?!\nThat looks too dangerous.\nIf I were to do that, you'd file an angry GitHub issue complaining that I deleted your hard drive.\nYou're on your own deleting this directory!" % abspath)
                    sys.exit(1)
                clear_directory(args.log_dir)
    
        ETHENO.logger.save_to_directory(args.log_dir)
        if not args.log_file:
            # Also create a unified log in the log dir:
            ETHENO.logger.save_to_file(os.path.join(args.log_dir, 'Complete.log'))
        
    # First, see if we need to install Echidna:
    if args.echidna:
        if not echidna_exists():
            if not ynprompt('Echidna does not appear to be installed.\nWould you like to have Etheno attempt to install it now? [yN] '):
                sys.exit(1)
            install_echidna()
            if not echidna_exists():
                ETHENO.logger.error('Etheno failed to install Echidna. Please install it manually https://github.com/trailofbits/echidna')
                sys.exit(1)
        
    if args.genesis is None:
        # Set defaults since no genesis was supplied
        if args.accounts is None:
            args.accounts = 10
        if args.gas_price is None:
            args.gas_price = 20000000000
        
    accounts = []

    if args.genesis:
        with open(args.genesis, 'rb') as f:
            genesis = json.load(f)
            if 'config' not in genesis:
                genesis['config'] = {}
            if 'alloc' not in genesis:
                genesis['alloc'] = {}
            if args.network_id is None:
                args.network_id = genesis['config'].get('chainId', None)
            for addr, bal in genesis['alloc'].items():
                pkey = None
                if 'privateKey' in bal:
                    pkey = bal['privateKey']
                accounts.append(Account(address = int(addr, 16), balance = decode_value(bal['balance']), private_key = decode_value(pkey)))
    else:
        # We will generate it further below once we've resolved all of the parameters
        genesis = None

    accounts += make_accounts(args.accounts, default_balance = int(args.balance * 1000000000000000000))

    if genesis is not None:
        # add the new accounts to the genesis
        for account in accounts[len(genesis['alloc']):]:
            genesis['alloc'][format_hex_address(account.address)] = {'balance': "%d" % account.balance, 'privateKey': format_hex_address(account.private_key), 'comment': '`privateKey` and `comment` are ignored.  In a real chain, the private key should _not_ be stored!'}

    if args.raw is None:
        args.raw = []

    if args.ganache and args.master:
        parser.print_help()
        sys.stderr.write('\nError: You cannot specify both --ganache and --master at the same time!\n')
        sys.exit(1)        
    elif args.ganache:
        if args.ganache_port is None:
            args.ganache_port = find_open_port(args.port + 1)

        if args.network_id is None:
            args.network_id = 0x657468656E6F # 'etheno' in hex

        ganache_accounts = ["--account=%s,0x%x" % (acct.private_key, acct.balance) for acct in accounts]

        ganache_instance = ganache.Ganache(args = ganache_accounts + ['-g', str(args.gas_price), '-i', str(args.network_id)], port=args.ganache_port)

        ETHENO.master_client = ganache.GanacheClient(ganache_instance)

        ganache_instance.start()
    elif args.master:
        ETHENO.master_client = AddressSynchronizingClient(RpcProxyClient(args.master))
    elif args.client and not args.geth and not args.parity:
        ETHENO.master_client = AddressSynchronizingClient(RpcProxyClient(args.client[0]))
        args.client = args.client[1:]
    elif args.raw and not args.geth and not args.parity:
        ETHENO.master_client = RawTransactionClient(RpcProxyClient(args.raw[0]), accounts)
        args.raw = args.raw[1:]
        
    if args.network_id is None:
        if ETHENO.master_client:
            args.network_id = int(ETHENO.master_client.post({
                'id': 1,
                'jsonrpc': '2.0',
                'method': 'net_version'
            })['result'], 16)
        else:
            args.network_id = 0x657468656E6F # 'etheno' in hex

    if genesis is None:
        genesis = make_genesis(network_id = args.network_id, accounts = accounts)
    else:
        # Update the genesis with any overridden values
        genesis['config']['chainId'] = args.network_id

    if args.save_genesis:
        with open(args.save_genesis, 'wb') as f:
            f.write(json.dumps(genesis).encode('utf-8'))
            ETHENO.logger.info("Saved genesis to %s" % args.save_genesis)

    if args.geth:
        if args.geth_port is None:
            args.geth_port = find_open_port(args.port + 1)

        geth_instance = geth.GethClient(genesis=genesis, port=args.geth_port)
        geth_instance.etheno = ETHENO
        for account in accounts:
            # TODO: Make some sort of progress bar here
            geth_instance.logger.info("Unlocking Geth account %s" % format_hex_address(account.address, True))
            geth_instance.import_account(account.private_key)
        geth_instance.start(unlock_accounts=True)
        if ETHENO.master_client is None:
            ETHENO.master_client = geth_instance
        else:
            ETHENO.add_client(AddressSynchronizingClient(geth_instance))

    if args.parity:
        if args.parity_port is None:
            if args.geth_port is not None:
                args.parity_port = find_open_port(args.geth_port + 1)
            else:
                args.parity_port = find_open_port(args.port + 1)

        parity_instance = parity.ParityClient(genesis=genesis, port=args.parity_port)
        parity_instance.etheno = ETHENO
        for account in accounts:
            # TODO: Make some sort of progress bar here
            parity_instance.import_account(account.private_key)
        parity_instance.start(unlock_accounts=True)
        if ETHENO.master_client is None:
            ETHENO.master_client = parity_instance
        else:
            ETHENO.add_client(AddressSynchronizingClient(parity_instance))        

    for client in args.client:
        ETHENO.add_client(AddressSynchronizingClient(RpcProxyClient(client)))

    for client in args.raw:
        ETHENO.add_client(RawTransactionClient(RpcProxyClient(client), accounts))

    manticore_client = None
    if args.manticore:
        manticore_client = ManticoreClient()
        ETHENO.add_client(manticore_client)
        if args.manticore_max_depth is not None:
            manticore_client.manticore.register_detector(manticoreutils.StopAtDepth(args.manticore_max_depth))
        manticore_client.manticore.verbosity(getattr(logger, args.log_level))
        manticore_client.reassign_manticore_loggers()

    if args.truffle:
        truffle_controller = truffle.Truffle(parent_logger=ETHENO.logger)
        def truffle_thread():
            if ETHENO.master_client:
                ETHENO.master_client.wait_until_running()
            ETHENO.logger.info("Etheno Started! Running Truffle...")
            ret = truffle_controller.run(args.truffle_args)
            if ret != 0:
                ETHENO.logger.error("Truffle exited with code %s" % ret)
                ETHENO.shutdown()
                # TODO: Propagate the error code elsewhere so Etheno doesn't exit with code 0

            for plugin in ETHENO.plugins:
                plugin.finalize()

            if manticore_client is not None:
                if args.manticore_script is not None:
                    exec(args.manticore_script.read(), {'manticore' : manticore_client.manticore, 'manticoreutils' : manticoreutils, 'logger' : logger.EthenoLogger(os.path.basename(args.manticore_script.name), parent=manticore_client.logger)})
                else:
                    manticoreutils.register_all_detectors(manticore_client.manticore)
                    manticore_client.multi_tx_analysis()
                    manticore_client.manticore.finalize()
                manticore_client.logger.info(manticore_client.manticore.global_findings)
                manticore_client.logger.info("Results are in %s" % manticore_client.manticore.workspace)
                ETHENO.shutdown()
            elif not ETHENO.clients and not ETHENO.plugins:
                ETHENO.logger.info("No clients or plugins running; exiting...")
                ETHENO.shutdown()

        thread = Thread(target=truffle_thread)
        thread.start()

    if args.run_differential and (ETHENO.master_client is not None) and next(filter(lambda c : not isinstance(c, ManticoreClient), ETHENO.clients), False):
        # There are at least two non-Manticore clients running
        ETHENO.logger.info("Initializing differential tests to compare clients %s" % ', '.join(map(str, [ETHENO.master_client] + ETHENO.clients)))
        ETHENO.add_plugin(DifferentialTester())

    if args.echidna:
        ETHENO.add_plugin(EchidnaPlugin(transaction_limit=args.fuzz_limit))

    had_plugins = len(ETHENO.plugins) > 0

    if ETHENO.master_client is None and not ETHENO.clients and not ETHENO.plugins:
        if not had_plugins:
            ETHENO.logger.info("No clients or plugins provided; exiting...")
        # else: this can also happen if there were plugins but they uninstalled themselves after running
        return

    etheno = EthenoView()
    app.add_url_rule('/', view_func=etheno.as_view('etheno'))

    etheno_thread = ETHENO.run(debug = args.debug, run_publicly = args.run_publicly, port = args.port)
    if args.truffle:
        truffle_controller.terminate()

    if args.log_file is not None:
        print("Log file saved to: %s" % os.path.realpath(args.log_file))
    if args.log_dir is not None:
        print("Logs %ssaved to: %s" % (['','also '][args.log_file is not None], os.path.realpath(args.log_dir)))

if __name__ == '__main__':
    main()
