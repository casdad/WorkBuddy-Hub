import argparse
import getpass
import os
import sys
import threading
import webbrowser
from pathlib import Path


def host_argument(value):
    host=value.strip().lower().rstrip('.')
    if not host:
        raise argparse.ArgumentTypeError('Host 不能为空')
    if any(ch in host for ch in '/@ '):
        raise argparse.ArgumentTypeError('Host 只写主机名，不要带协议、路径或空格')
    # TrustedHost matches the Host header without its port, and a bracketed IPv6
    # literal must keep its brackets.
    if host.startswith('['):
        end=host.find(']')
        return host[:end+1] if end>0 else host
    return host.split(':',1)[0]


def main():
    parser=argparse.ArgumentParser(description='WorkBuddy Hub 本地账号与 API 管理系统')
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--port',type=int,default=8792)
    parser.add_argument('--allow-host',action='append',default=[],type=host_argument,metavar='HOST',
                        help='额外允许的 Host 头，可重复；支持 *.example.com。默认仅回环，服务仍只监听 127.0.0.1')
    parser.add_argument('--no-open',action='store_true')
    parser.add_argument('--no-tray',action='store_true')
    parser.add_argument('--restore',type=Path,help='恢复加密备份到一个新的 --data-dir')
    parser.add_argument('--check',action='store_true',help='离线自检后退出')
    args=parser.parse_args()
    frozen=getattr(sys,'frozen',False)
    default=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'WorkBuddyHub' if frozen else Path(__file__).parent/'data'
    folder=(args.data_dir or default).resolve()
    if args.restore:
        from hub.security import restore_backup
        restore_backup(args.restore.read_bytes(),getpass.getpass('备份密码：'),folder)
        print('恢复完成：',folder)
        return
    folder.mkdir(parents=True,exist_ok=True)
    # Lock the data directory for the whole process lifetime.
    lock=open(folder/'instance.lock','a+b')
    if os.fstat(lock.fileno()).st_size==0:
        lock.write(b'0');lock.flush()
    lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        raise SystemExit('该数据目录已有实例运行，请使用托盘打开管理台。')
    from hub.api import create_app
    static=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'web'/'dist'
    app=create_app(folder,password=os.environ.get('WBH_ADMIN_PASSWORD'),static=static,allowed_hosts=args.allow_host)
    if args.check:
        from hub import __version__
        schema=app.state.hub.store.one('PRAGMA user_version')['user_version']
        print('WorkBuddy Hub '+__version__+'; schema='+str(schema)+'; vault=ok; frontend='+str(static.exists()))
        return
    import uvicorn
    url=f'http://127.0.0.1:{args.port}'
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,log_level='warning',access_log=False))
    def serve():
        server.run()
    worker=threading.Thread(target=serve,daemon=True)
    worker.start()
    import time
    for _ in range(200):
        if server.started or not worker.is_alive():
            break
        time.sleep(.05)
    if not server.started:
        raise SystemExit('服务启动失败，请检查端口是否已被使用')
    print('管理台：',url,'\n数据目录：',folder,'\n首次密码：',folder/'initial-admin.txt')
    if args.allow_host:
        print('额外允许的 Host：',', '.join(args.allow_host),'（服务仍只监听 127.0.0.1，请通过反向代理访问）')
    if not args.no_open:
        webbrowser.open(url)
        initial=folder/'initial-admin.txt'
        if initial.exists() and os.name=='nt':
            os.startfile(initial)
    try:
        if not args.no_tray:
            import pystray
            from PIL import Image,ImageDraw
            icon=Image.new('RGB',(64,64),'#1e3a8a')
            draw=ImageDraw.Draw(icon)
            draw.rounded_rectangle((12,14,52,50),radius=8,fill='#dbeafe')
            draw.rectangle((21,25,27,39),fill='#1e3a8a')
            draw.rectangle((37,25,43,39),fill='#1e3a8a')
            def exit_app(icon,item):
                server.should_exit=True
                icon.stop()
            menu=pystray.Menu(pystray.MenuItem('打开管理台',lambda: webbrowser.open(url),default=True),
                              pystray.MenuItem('打开数据目录',lambda: os.startfile(folder)),
                              pystray.MenuItem('退出',exit_app))
            pystray.Icon('WorkBuddyHub',icon,'WorkBuddy Hub · 本地运行',menu).run()
        else:
            while worker.is_alive():
                worker.join(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit=True
        worker.join(15)
        lock.close()


if __name__=='__main__':
    main()
