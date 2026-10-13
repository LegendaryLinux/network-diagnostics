"""Run with python -m netdiag."""
import argparse
import asyncio
import getpass
import os
import sys
import warnings


def main():
    parser = argparse.ArgumentParser(description='Continuous network diagnostics with indefinite granular history')
    commands = parser.add_subparsers(dest='command',required=True)
    collect = commands.add_parser('collect',help='Collect until Ctrl+C; serve local dashboard')
    collect.add_argument('--config',help='JSON configuration (defaults work for public-service checks)')
    collect.add_argument('--no-dashboard',action='store_true',help='Only collect; dashboard can run separately')
    collect.add_argument('--ask-passphrase',action='store_true',help='Prompt and replace the remembered remote key, overriding NETDIAG_KEY')
    dashboard = commands.add_parser('dashboard',help='Inspect an existing dataset without collecting')
    dashboard.add_argument('--db',default='data/netdiag.sqlite3')
    dashboard.add_argument('--port',type=int,default=8765)
    args = parser.parse_args()
    try:
        if args.command=='collect':
            from .config import load_config
            from .collector import collect as run
            config = load_config(args.config)
            key = None
            if config['remote']:
                from .protocol import derive_key,load_key
                from .credentials import load_saved_key,save_key
                if not args.ask_passphrase and os.environ.get('NETDIAG_KEY'):
                    key = load_key()
                elif not args.ask_passphrase:
                    key = load_saved_key(config['remote'])
                if key is None:
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter('error',getpass.GetPassWarning)
                            key = derive_key(getpass.getpass('Diagnostic shared passphrase: '))
                    except getpass.GetPassWarning:
                        raise ValueError('Cannot read a hidden passphrase without a usable terminal; supply NETDIAG_KEY for unattended collection') from None
                    except (EOFError,KeyboardInterrupt):
                        raise ValueError('Passphrase entry cancelled; collection was not started') from None
                    save_key(config['remote'],key)
            asyncio.run(run(config,serve_dashboard=not args.no_dashboard,key=key))
        else:
            from .dashboard import create_server
            from .storage import Reader
            Reader(args.db).metadata()
            server = create_server(args.db,port=args.port)
            print('Dashboard: http://127.0.0.1:'+str(args.port),flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(type(error).__name__+': '+str(error),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
