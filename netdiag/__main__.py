"""Run with python -m netdiag."""
import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(description='Continuous network diagnostics with indefinite granular history')
    commands = parser.add_subparsers(dest='command',required=True)
    collect = commands.add_parser('collect',help='Collect until Ctrl+C; serve local dashboard')
    collect.add_argument('--config',help='JSON configuration (defaults work for gateway and public checks)')
    collect.add_argument('--no-dashboard',action='store_true',help='Only collect; dashboard can run separately')
    dashboard = commands.add_parser('dashboard',help='Inspect an existing dataset without collecting')
    dashboard.add_argument('--db',default='data/netdiag.sqlite3')
    dashboard.add_argument('--port',type=int,default=8765)
    args = parser.parse_args()
    try:
        if args.command=='collect':
            from .config import load_config
            from .collector import collect as run
            asyncio.run(run(load_config(args.config),serve_dashboard=not args.no_dashboard))
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
